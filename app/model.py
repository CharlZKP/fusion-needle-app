"""The model server: an HTTP client for the step server, and the child process that runs it.

The app never imports needle or loads weights (README.md); it starts
`python -m stepserver.cli serve` on a free port with a token it generates, or talks to
a server that is already running.

The server is started through `supervisor.py`, which stops it (and its engine
workers) when this process ends for any reason, a hard kill included. Whatever
an earlier launch still left behind is stopped at the first start
(`supervisor.cleanup_stale`).
"""
from __future__ import annotations

import collections
import contextlib
import json
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import modelstore, paths, supervisor


class ModelError(Exception):
    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


class ModelClient:
    def __init__(self, url: str, token: str = "", timeout: float = 900.0):
        self.url = url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _call(self, method: str, path: str, body: dict | None = None, timeout: float | None = None) -> dict:
        headers = {"Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(self.url + path, data=data, headers=headers, method=method)
        try:
            with self._opener.open(request, timeout=timeout or self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as failure:
            text = failure.read().decode("utf-8", "replace")
            try:
                message = json.loads(text).get("error") or text
            except (ValueError, AttributeError):
                message = text
            raise ModelError(f"model server: {message}", failure.code) from failure
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError, ValueError) as failure:
            reason = getattr(failure, "reason", failure)
            raise ModelError(f"model server not reachable at {self.url}: {reason}") from failure

    def health(self, timeout: float = 5.0) -> dict:
        return self._call("GET", "/health", timeout=timeout)

    def tools(self) -> dict:
        return self._call("GET", "/v1/tools", timeout=30)

    def step(self, query: str, system: str, tools: list[str] | None) -> dict:
        body = {"query": query, "system": system}
        if tools:
            body["tools"] = list(tools)
        return self._call("POST", "/v1/step", body)


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def child_python(configured: str = "") -> str:
    """Interpreter for the step server: the configured one, this one, else the repo venv."""
    if configured:
        return configured
    if paths.FROZEN:
        return sys.executable                      # the exe dispatches `-m stepserver.cli` (launcher.py)
    try:
        import importlib.util

        if importlib.util.find_spec("stepserver") is not None:
            return sys.executable
    except (ImportError, ValueError):
        pass
    root = paths.resource_root()
    for candidate in (root / ".venv" / "Scripts" / "python.exe", root / ".venv" / "bin" / "python"):
        if candidate.is_file():
            return str(candidate)
    return sys.executable


def weights_args(project: str, weights: str) -> list[str]:
    """'base', a .cact path, or a run id under projects/<project>/models/."""
    weights = (weights or "base").strip()
    if weights == "base" or weights.lower().endswith(".cact") or os.path.isfile(weights):
        return ["--weights", weights]
    if (paths.project_dir(project) / "models" / weights).is_dir():
        return ["--run-id", weights]
    raise ModelError(f"weights {weights!r}: not 'base', not a .cact file, and no models/{weights}/ in {project}")


class ModelManager:
    """Owns the model connection for the app: a server it starts, or one that is already running."""

    def __init__(self, settings, log_path: Path | None = None):
        self.settings = settings
        self.state = "stopped"            # stopped | starting | ready | error
        self.detail = ""
        self.error_kind = ""              # "" or "start"
        self.mode = ""
        self.client: ModelClient | None = None
        self.health: dict = {}
        self.catalogue: list[dict] = []
        self.max_tools = 5
        self.process: subprocess.Popen | None = None
        self.log = collections.deque(maxlen=200)
        self.log_path = log_path
        self._lock = threading.RLock()
        self._generation = 0
        self._cleaned = False
        self.pid_file: Path | None = None
        self.token = ""                   # of the server this app started (never shown, never exported)

    # ---- public --------------------------------------------------------
    def start_async(self):
        with self._lock:
            self._generation += 1
            generation = self._generation
        thread = threading.Thread(target=self._start, args=(generation,), name="model-start", daemon=True)
        thread.start()
        return thread

    def restart(self):
        self.stop()
        return self.start_async()

    def stop(self):
        with self._lock:
            self._generation += 1
            process, self.process = self.process, None
            self.client = None
            self.state, self.detail = "stopped", ""
        if process is not None:
            _terminate(process, self.pid_file)

    def snapshot(self) -> dict:
        return {"state": self.state, "detail": self.detail, "error_kind": self.error_kind, "mode": self.mode,
                "url": self.client.url if self.client else "", "managed": self.process is not None,
                "pid": self.process.pid if self.process is not None else None,
                "server_pid": self.server_pid(),
                "model": self.health.get("model", ""), "tuned": self.health.get("tuned"),
                "needle_version": self.health.get("needle_version", ""),
                "tools": len(self.catalogue), "max_tools_per_step": self.max_tools,
                "log": list(self.log)[-40:]}

    def server_pid(self) -> int | None:
        """Pid of the step server itself (``pid`` in the snapshot is its supervisor), from the pid file."""
        if self.process is None or self.pid_file is None:
            return None
        try:
            with open(self.pid_file, encoding="utf-8") as handle:
                return json.load(handle).get("server_pid")
        except (OSError, ValueError, AttributeError):
            return None

    def cleanup_stale(self):
        """Once per launch: stop servers that a crashed launch of this app left running."""
        if self._cleaned:
            return
        self._cleaned = True
        try:
            supervisor.cleanup_stale(supervisor.run_dir(paths.config_dir()), self.log.append)
        except Exception as failure:                # never let this stop the start
            self.log.append(f"stale-server check failed: {type(failure).__name__}: {failure}")

    def poll(self):
        """Called from the status thread: notice a dead child or a server that went away."""
        client, process = self.client, self.process
        if self.state != "ready" or client is None:
            return
        if process is not None and process.poll() is not None:
            self._fail(f"the model server exited (code {process.returncode}); see the model log", "start")
            return
        try:
            self.health = client.health()
        except ModelError as failure:
            if process is None:
                self._fail(str(failure), "start")

    # ---- start ---------------------------------------------------------
    def effective_mode(self) -> str:
        mode = self.settings["model"]["mode"]
        return "spawn" if mode == "auto" else mode

    def _current(self, generation: int) -> bool:
        return generation == self._generation

    def _fail(self, message: str, kind: str = "start"):
        self.state, self.detail, self.error_kind = "error", message, kind

    def _start(self, generation: int):
        model = self.settings["model"]
        self.mode = self.effective_mode()
        self.state, self.detail, self.error_kind = "starting", "", ""
        self.health, self.catalogue = {}, []
        self.cleanup_stale()
        try:
            if self.mode == "url":
                if not model["url"]:
                    raise ModelError("no server URL set (Settings, Model)")
                client = ModelClient(model["url"], model["token"])
                self.detail = f"connecting to {client.url}"
            else:
                client = self._spawn(self._weights(model, generation), generation)
            self._wait_ready(client, generation)
        except ModelError as failure:
            if self._current(generation):
                self._fail(str(failure))
                self._reap()
        except Exception as failure:                    # a bug here must not leave "starting" forever
            if self._current(generation):
                self._fail(f"{type(failure).__name__}: {failure}")
                self._reap()

    def _reap(self):
        process, self.process = self.process, None
        if process is not None:
            _terminate(process, self.pid_file)

    def _weights(self, model: dict, generation: int) -> list[str]:
        """'auto': the published model, downloaded once into the per-user data folder; else as configured."""
        weights = (model["weights"] or "auto").strip()
        if weights != "auto":
            return weights_args(self.settings["project"], weights)

        def progress(done: int, total: int, name: str):
            if not self._current(generation):
                raise modelstore.Cancelled()
            self.detail = modelstore.progress_text(done, total, name)

        self.detail = "looking for the model"
        try:
            if model["source"] == "custom" and model["custom"]["repo"]:      # the user's own choice (Settings)
                path = modelstore.ensure_custom(model["custom"], token=model["hf_token"], progress=progress,
                                                note=self.log.append)
            else:
                path = modelstore.ensure_model(progress=progress, note=self.log.append)
        except modelstore.Cancelled as failure:
            raise ModelError("superseded") from failure
        except modelstore.StoreError as failure:
            raise ModelError(str(failure)) from failure
        return ["--weights", str(path)]

    def _spawn(self, weight_args: list[str], generation: int) -> ModelClient:
        model = self.settings["model"]
        port = free_port()
        token = secrets.token_urlsafe(32)
        root = paths.resource_root()
        command = [child_python(model["python"]), "-m", "stepserver.cli", "serve",
                   "--project", self.settings["project"], *weight_args,
                   "--host", "127.0.0.1", "--port", str(port), "--agents", str(model["agents"]), "--quiet"]
        if not model["warm"]:
            command.append("--no-warm")
        env = dict(os.environ)
        env["STEPSERVER_TOKEN"] = token
        env["STEPSERVER_ROOT"] = str(root)
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        env.setdefault("NEEDLE_TELEMETRY", "0")
        shown = " ".join(command)
        self.log.append(f"$ {shown}")
        self.detail = "starting the model server"
        pid_file = supervisor.run_dir(paths.config_dir()) / f"server-{os.getpid()}-{secrets.token_hex(4)}.json"
        wrapped = supervisor_command(command, pid_file)
        kwargs = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            # stdin is the control pipe: it closes when this process ends, and the supervisor then stops the server
            process = subprocess.Popen(wrapped, cwd=str(root), env=env, stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       text=True, encoding="utf-8", errors="replace", **kwargs)
        except OSError as failure:
            raise ModelError(f"could not start the model server ({wrapped[0]}): {failure}") from failure
        problem = supervisor.bind_to_this_process(process)      # Windows: Job Object, kill on close
        if problem:
            self.log.append(problem)
        with self._lock:
            if not self._current(generation):
                _terminate(process, pid_file)
                raise ModelError("superseded")
            self.process, self.pid_file, self.token = process, pid_file, token
        threading.Thread(target=self._pump, args=(process,), name="model-log", daemon=True).start()
        return ModelClient(f"http://127.0.0.1:{port}", token)

    def _pump(self, process: subprocess.Popen):
        handle = None
        if self.log_path is not None:
            with contextlib.suppress(OSError):
                self.log_path.parent.mkdir(parents=True, exist_ok=True)
                handle = open(self.log_path, "a", encoding="utf-8")
        try:
            for line in process.stdout:
                line = line.rstrip()
                self.log.append(line)
                if handle is not None:
                    handle.write(line + "\n")
                    handle.flush()
        except (OSError, ValueError):
            pass
        finally:
            if handle is not None:
                handle.close()

    def _wait_ready(self, client: ModelClient, generation: int, timeout: float = 1800.0):
        deadline = time.time() + timeout
        last = ""
        while self._current(generation):
            process = self.process
            if process is not None and process.poll() is not None:
                tail = " | ".join(list(self.log)[-3:])
                raise ModelError(f"the model server exited (code {process.returncode}): {tail}")
            try:
                health = client.health(timeout=3)
                listing = client.tools()
                catalogue = listing.get("tools")
                if not isinstance(catalogue, list) or not catalogue:
                    raise ModelError("the model server returned an empty tool catalogue")
                if not self._current(generation):
                    return
                self.health, self.catalogue = health, catalogue
                self.max_tools = int(listing.get("max_tools_per_step") or health.get("max_tools_per_step") or 5)
                self.client = client
                self.state, self.detail = "ready", ""
                return
            except ModelError as failure:
                if failure.status in (401, 403):
                    raise ModelError("the model server refused the token (Settings, Model)") from failure
                last = str(failure)
            if time.time() > deadline:
                raise ModelError(f"the model server did not come up in {timeout:.0f} s: {last}")
            time.sleep(0.4)


def supervisor_command(server_command: list[str], pid_file: Path) -> list[str]:
    """The server command wrapped in the supervisor, which runs under this app's own interpreter."""
    if paths.FROZEN:
        head = [sys.executable, "-m", "app.supervisor"]         # fusion_needle.py dispatches `-m`
    else:
        head = [sys.executable, str(Path(supervisor.__file__).resolve())]
    return head + ["--owner-pid", str(os.getpid()), "--pid-file", str(pid_file), "--", *server_command]


def _terminate(process: subprocess.Popen, pid_file: Path | None = None):
    """Stop the supervisor and, through it, the step server and its engine workers.

    Closing the control pipe is the request: the supervisor sends the server a
    Ctrl+C (serve() closes the engine on KeyboardInterrupt), waits, then takes
    the whole process group down. If the supervisor does not end in time, the
    recorded server and the supervisor are stopped from here.
    """
    if process.poll() is not None:
        return
    with contextlib.suppress(OSError, ValueError):
        if process.stdin is not None:
            process.stdin.close()
    try:
        process.wait(timeout=supervisor.GRACE + 7)
        return
    except (subprocess.TimeoutExpired, OSError):
        pass
    server = None
    if pid_file is not None:
        with contextlib.suppress(OSError, ValueError, AttributeError):
            with open(pid_file, encoding="utf-8") as handle:
                record = json.load(handle)
            if supervisor.is_recorded_process(record.get("server_pid"), record.get("server_start"),
                                              record.get("server_command")):
                server = record["server_pid"]
    with contextlib.suppress(OSError):
        process.kill()
    if server is not None:
        supervisor.kill_tree(server)
    with contextlib.suppress(subprocess.TimeoutExpired, OSError):
        process.wait(timeout=5)
    if pid_file is not None:
        with contextlib.suppress(OSError):
            pid_file.unlink()
