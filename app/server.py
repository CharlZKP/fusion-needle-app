"""The app object and the local HTTP server behind the single-page UI.

Everything binds to 127.0.0.1. The UI gets a per-launch token in the page and
sends it on every /api call, and the Host header must be a loopback name, so
another site open in a browser cannot drive Fusion through this port.
"""
from __future__ import annotations

import base64
import binascii
import hmac
import json
import mimetypes
import os
import secrets
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import APP_NAME, __version__, paths, phrases, selfcheck
from .backend import Backend, BackendError
from .diagnostics import Diagnostics
from .goal import split_goal
from .guard import Guard, Rules
from .history import NO_CALL_REASONS, SessionLog
from .mcp import FusionClient
from .model import ModelError, ModelManager, child_python
from .session import ActionError, Busy, Session

try:                                                   # builds that download their model have it; this workspace does not
    from . import modelstore
except ImportError:
    modelstore = None

MAX_BODY = 1 << 20
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "[::1]")


class ModelPort:
    """What the session needs from the model, read from the manager at call time."""

    def __init__(self, manager: ModelManager):
        self.manager = manager

    def step(self, query, system, names):
        client = self.manager.client
        if client is None or self.manager.state != "ready":
            raise ModelError("the model is not ready")
        return client.step(query, system, names)

    def catalogue(self):
        return self.manager.catalogue

    def max_tools(self):
        return self.manager.max_tools

    def name(self):
        """The served model's name; a custom model says so, so its history rows can be told apart."""
        name = self.manager.health.get("model", "")
        model = self.manager.settings["model"]
        if name and custom_active(self.manager.settings):
            return f"{name} [custom: {model['custom']['repo']}@{model['custom']['revision']}]"
        return name


def custom_active(settings) -> bool:
    """Is the model in use one the user chose from another Hugging Face repository?"""
    model = settings["model"]
    return (modelstore is not None and model["source"] == "custom" and model["mode"] != "url"
            and (model["weights"] or "auto").strip() == "auto" and bool(model["custom"]["repo"]))


def engine_version() -> str:
    try:
        from importlib import metadata
        return metadata.version("cactus-needle")
    except Exception:                                    # not installed as a distribution (some frozen builds)
        return ""


class App:
    def __init__(self, settings, *, start_model: bool = True):
        self.settings = settings
        self.token = secrets.token_urlsafe(24)
        self.started = time.time()
        self.stop_event = threading.Event()
        self.start_model = start_model
        self.project = settings["project"]
        self.backend: Backend | None = None
        self.backend_error = ""
        try:
            self.backend = Backend(paths.project_dir(self.project))
        except BackendError as failure:
            self.backend_error = str(failure)
        self.model = ModelManager(settings, log_path=paths.config_dir() / "model-server.log")
        self.diagnostics = Diagnostics(paths.config_dir() / "diagnostics", keep=settings["diagnostics_keep"])
        self.fusion = FusionClient(settings["fusion_url"], recorder=self.diagnostics.record_call)
        self.fusion_check: dict | None = None            # the last "Check Fusion" result
        self.last_export = ""
        self.fusion_status = {"state": "offline", "detail": "not checked yet", "server": ""}
        log_dir = Path(settings["log_dir"]) if settings["log_dir"] else paths.default_log_dir(self.project)
        self.log = SessionLog(log_dir)
        self.validator = None
        self.rules = Rules.load(paths.project_dir(self.project))         # synonyms, state needs, groups
        self.examples = phrases.Examples.load(paths.UI_DIR / "examples.json")
        self.session = Session(fusion=self.fusion, model=ModelPort(self.model), backend=self.backend,
                               log=self.log, settings=settings.data, validator=self.validator,
                               export_dir=self._export_dir(), diagnostics=self.diagnostics,
                               guard=Guard(self.rules), examples=self.examples)
        self.check_result: dict | None = None
        self._closed = False
        self._status_thread: threading.Thread | None = None
        self.httpd: ThreadingHTTPServer | None = None

    # ---- lifecycle -----------------------------------------------------
    def _export_dir(self) -> str:
        return self.settings["export_dir"] or str(paths.default_export_dir())

    def start(self):
        if self.start_model:
            self.model.start_async()
        self._status_thread = threading.Thread(target=self._watch, name="status", daemon=True)
        self._status_thread.start()

    def _watch(self):
        was_connected = False
        while not self.stop_event.is_set():
            try:
                self.model.poll()
                if not self.session.busy:
                    self._check_fusion()
                    connected = self.fusion_status["state"] == "connected"
                    if connected and not was_connected and self.backend is not None and not self.session.busy:
                        try:
                            self.session.refresh()
                        except ActionError:
                            pass
                    was_connected = connected
            except Exception as failure:                 # the watcher must survive anything
                self.fusion_status = {"state": "offline", "detail": f"{type(failure).__name__}: {failure}",
                                      "server": ""}
            self.stop_event.wait(3.0)

    def _check_fusion(self):
        if self.fusion.alive():
            info = self.fusion.server_info
            name = " ".join(str(part) for part in (info.get("name"), info.get("version")) if part)
            self.fusion_status = {"state": "connected", "detail": "", "server": name}
        else:
            self.fusion_status = {"state": "offline", "server": "",
                                  "detail": "Start Fusion and enable its MCP server, then this connects by itself."}

    def shutdown(self):
        if self._closed:
            return
        self._closed = True
        self.stop_event.set()
        try:
            self.session.close()                         # writes the pending row
        finally:
            self.model.stop()
            if self.httpd is not None:
                threading.Thread(target=self.httpd.shutdown, daemon=True).start()

    # ---- state for the UI ----------------------------------------------
    def state(self) -> dict:
        return {
            "app": {"name": APP_NAME, "version": __version__, "project": self.project,
                    "frozen": paths.FROZEN, "config": str(self.settings.path),
                    "export_dir": self._export_dir(), "settings_error": self.settings.load_error,
                    "custom_model": modelstore is not None, "engine": engine_version()},
            "fusion": {**self.fusion_status, "url": self.settings["fusion_url"]},
            "model": {**self.model.snapshot(), "effective_mode": self.model.effective_mode(),
                      **self.model_source()},
            "backend": {"ok": self.backend is not None, "error": self.backend_error,
                        "router": bool(self.backend and self.backend.has_router),
                        "router_error": self.backend.router_error if self.backend else "",
                        "call_info": bool(self.backend and hasattr(self.backend.render_module, "call_info"))},
            "session": self.session.snapshot(),
            "guard": {"problems": list(self.rules.problems) + ([self.examples.problem] if self.examples.problem else [])},
            "settings": self.settings.public(),
            "no_call_reasons": list(NO_CALL_REASONS),
            "check": self.check_result,
            "fusion_check": self.fusion_check,
            "diagnostics": {"folder": str(self.diagnostics.directory), "last_export": self.last_export,
                            "calls": len(self.diagnostics.calls), "capacity": self.diagnostics.calls.maxlen},
        }

    def tools(self) -> dict:
        tools = []
        for tool in self.model.catalogue:
            info = self.backend.call_info(tool["name"]) if self.backend else {"confirm": False, "read_only": False}
            tools.append({**tool, "info": info})
        return {"tools": tools, "max_tools_per_step": self.model.max_tools}

    def help(self) -> dict:
        """The "What can I say?" panel: the model server's catalogue, or the project's own file before it is up."""
        return phrases.help_panel(self.model.catalogue or self.rules.catalogue, self.rules, self.examples)

    # ---- which model: the published one, or the user's own from Hugging Face ----
    def model_source(self) -> dict:
        model = self.settings["model"]
        custom = model["custom"]
        active = custom_active(self.settings)
        return {"source": "custom" if active else "official",
                "official_repo": modelstore.source()["repo"] if modelstore is not None else "",
                "custom": {key: custom[key] for key in ("repo", "revision", "file", "bytes")},
                "custom_checked": bool(custom["sha256"])}

    def _store(self):
        if modelstore is None:
            raise ActionError("this build does not download models, so the model source cannot be changed here")
        return modelstore

    def check_model_source(self, body: dict) -> dict:
        """Parse what the user pasted and ask the repository what it holds. Changes nothing."""
        store = self._store()
        token = str(body.get("token") or "") or self.settings["model"]["hf_token"]
        try:
            spec = store.parse_source(body.get("text"))
            if body.get("file") and not spec["file"]:
                spec = store.check_spec(spec["repo"], spec["revision"], body.get("file"))
            found = store.resolve_custom(spec, token)
        except store.StoreError as failure:
            raise ActionError(str(failure)) from failure
        return {**found, "engine": engine_version(), "host": store.source()["endpoint"].split("://", 1)[-1]}

    def use_custom_model(self, body: dict):
        """Switch to a custom model. Only with ``confirm: true``, which the page sends from its warning dialog."""
        if body.get("confirm") is not True:
            raise ActionError("switching to a custom model needs your confirmation (the warning dialog)")
        store = self._store()
        if self.session.busy:
            raise Busy("wait for the running step before changing the model")
        token = str(body.get("token") or "")
        try:
            spec = store.check_spec(body.get("repo"), body.get("revision"), body.get("file"))
            if not spec["file"]:
                raise store.StoreError("choose the model file first")
            found = store.resolve_custom(spec, token or self.settings["model"]["hf_token"])
        except store.StoreError as failure:
            raise ActionError(str(failure)) from failure
        model = {"source": "custom", "mode": "auto", "weights": "auto",
                 "custom": {key: found[key] for key in ("repo", "revision", "file", "bytes", "sha256")}}
        if token:
            model["hf_token"] = token
        elif body.get("token_clear") is True:
            model["hf_token"] = ""
        self.settings.update({"model": model})
        self.session.settings = self.settings.data
        self.diagnostics.event("model_source", source="custom", repo=found["repo"], revision=found["revision"],
                               file=found["file"], checked_against_manifest=found["verified"])
        if self.start_model:
            self.model.restart()
        return {"ok": True, **self.model_source()}

    def use_official_model(self):
        """Back to the published model. Its file is still in its own folder, so nothing is downloaded again."""
        self._store()
        if self.session.busy:
            raise Busy("wait for the running step before changing the model")
        self.settings.update({"model": {"source": "official"}})
        self.session.settings = self.settings.data
        self.diagnostics.event("model_source", source="official")
        if self.start_model:
            self.model.restart()
        return {"ok": True, **self.model_source()}

    # ---- actions -------------------------------------------------------
    def apply_settings(self, changes: dict):
        if not isinstance(changes, dict):
            raise ActionError("settings must be an object")
        if self.session.busy:
            raise Busy("wait for the running step before changing settings")
        before = json.dumps([self.settings["model"], self.settings["project"]], sort_keys=True)
        fusion_before = self.settings["fusion_url"]
        model = changes.get("model")
        if isinstance(model, dict):
            for key in ("source", "custom", "hf_token"):    # only the confirmed "Model source" actions set these
                model.pop(key, None)
            if model.get("token") in ("", None) and not model.get("token_clear"):
                model.pop("token", None)                 # an empty secret field means "keep"
            elif model.get("token_clear"):
                model["token"] = ""
        self.settings.update(changes)
        self.session.settings = self.settings.data
        self.session.export_dir = self._export_dir()
        if self.settings["fusion_url"] != fusion_before:
            self.fusion = FusionClient(self.settings["fusion_url"], recorder=self.diagnostics.record_call)
            self.session.fusion = self.fusion
            self.fusion_status = {"state": "offline", "detail": "connecting", "server": ""}
        after = json.dumps([self.settings["model"], self.settings["project"]], sort_keys=True)
        if after != before and self.start_model:
            self.model.restart()

    def check_log(self) -> dict:
        """Where this session's history file is and how many entries it holds."""
        self.session.commit_pending()
        self.check_result = {"file": str(self.log.path), "rows": self.log.rows_written, "errors": 0,
                             "warnings": 0, "issues": []}
        return self.check_result

    # ---- evidence from a real Fusion ------------------------------------
    def check_fusion(self):
        """Read-only self-check (selfcheck.py). Runs on the session worker, so it never overlaps a step."""
        self.need_backend()

        def run():
            self.fusion_check = {"running": True, "items": [], "overall": ""}
            try:
                report = selfcheck.run_check(self.fusion, self.backend, keep_images=self.session._keep_images,
                                             progress=lambda label: self.session._set(activity=f"Check Fusion: {label}"))
            except Exception as failure:                 # the report itself is the evidence: never lose it
                report = {"overall": "fail", "items": [{"id": "check", "label": "Check Fusion", "status": "fail",
                                                        "detail": f"{type(failure).__name__}: {failure}"}],
                          "summary": {}}
            report["running"] = False
            report["app_version"] = __version__
            self.fusion_check = report
            self.diagnostics.event("fusion_check", **report)

        self.session.run_exclusive("Check Fusion", run)

    def _secrets(self) -> list[str]:
        model = self.settings["model"]
        return [model["token"], model["hf_token"], self.model.token, self.token]

    def export_diagnostics(self) -> dict:
        """One zip in the diagnostics folder for the owner to send back. The server token is left out."""
        model = self.model.snapshot()
        info = {
            "app": {"name": APP_NAME, "version": __version__, "frozen": paths.FROZEN, "project": self.project,
                    "install_dir": str(paths.install_dir()), "config_dir": str(paths.config_dir())},
            "engine": {"needle_version": model["needle_version"], "model": model["model"], "tuned": model["tuned"],
                       "state": model["state"], "detail": model["detail"], "mode": model["mode"],
                       "tools": model["tools"], "health": self.model.health, **self.model_source()},
            "fusion": {**self.fusion_status, "url": self.settings["fusion_url"],
                       "server_info": self.fusion.server_info, "protocol": self.fusion.protocol},
            "backend": {"ok": self.backend is not None, "error": self.backend_error,
                        "dir": str(self.backend.dir) if self.backend else "",
                        "router": bool(self.backend and self.backend.has_router)},
            "settings": self.settings.public(),
        }
        files = {"session.json": json.dumps(self.session.snapshot(), ensure_ascii=False, indent=2, default=str),
                 "model-server.log": "\n".join(self.model.log)}
        if self.fusion_check is not None:
            files["fusion-check.json"] = json.dumps(self.fusion_check, ensure_ascii=False, indent=2, default=str)
        try:
            path = self.diagnostics.export(info, files, secrets=self._secrets())
        except OSError as failure:
            raise ActionError(f"could not write the diagnostics: {failure}") from failure
        self.last_export = str(path)
        return {"ok": True, "path": str(path), "folder": str(path.parent)}

    def open_folder(self, which: str):
        target = (Path(self._export_dir()) if which == "exports"
                  else self.diagnostics.directory if which == "diagnostics" else self.log.directory)
        target.mkdir(parents=True, exist_ok=True)
        try:
            if sys.platform == "win32":
                os.startfile(str(target))                # noqa: S606 - a folder the app owns
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(target)])
            else:
                subprocess.Popen(["xdg-open", str(target)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as failure:
            raise ActionError(f"could not open {target}: {failure}") from failure

    def need_backend(self):
        if self.backend is None:
            raise ActionError(f"the project backend did not load: {self.backend_error}")

    def need_ready(self):
        self.need_backend()
        if self.model.state != "ready":
            raise ActionError("the model is not ready yet")
        if self.fusion_status["state"] != "connected":
            self._check_fusion()
            if self.fusion_status["state"] != "connected":
                raise ActionError("Fusion is not connected")

    def dispatch(self, action: str, body: dict):
        session = self.session
        if action == "split":
            return {"features": split_goal(str(body.get("goal", "")))}
        if action == "goal":
            self.need_backend()
            session.set_goal(str(body.get("goal", "")))
        elif action == "run":
            self.need_ready()
            session.run(bool(body.get("auto")))
        elif action == "stop":
            session.stop()
        elif action == "approve":
            self.need_ready()
            session.approve(body.get("index"), body.get("calls"), override=body.get("override") is True)
        elif action == "rewrite":
            self.need_ready()
            session.rewrite(body.get("index"), str(body.get("text", "")))
        elif action == "accept_empty":
            session.accept_empty(body.get("index"), str(body.get("category", "")))
        elif action == "skip":
            session.skip(body.get("index"))
        elif action == "resume":
            self.need_ready()
            session.resume(body.get("index"))
        elif action == "proceed":
            self.need_ready()
            session.proceed(body.get("index"))
        elif action == "check_fusion":
            self.check_fusion()
        elif action == "diagnostics_export":
            return self.export_diagnostics()
        elif action == "choose":
            self.need_ready()
            session.choose(body.get("index"), body.get("id"))
        elif action == "retry":
            self.need_ready()
            session.retry(body.get("index"))
        elif action == "undo_step":
            session.undo_last_step()
        elif action == "undo_once":
            self.need_backend()
            session.undo_once()
        elif action == "refresh":
            self.need_backend()
            session.refresh()
        elif action == "settings":
            self.apply_settings(body)
        elif action == "model_source_check":
            return self.check_model_source(body)
        elif action == "model_source_use":
            return self.use_custom_model(body)
        elif action == "model_source_official":
            return self.use_official_model()
        elif action == "model_restart":
            if session.busy:
                raise Busy("wait for the running step")
            self.model.restart()
        elif action == "check_log":
            return self.check_log()
        elif action == "open_folder":
            self.open_folder(str(body.get("which", "log")))
        elif action == "quit":
            threading.Thread(target=self.shutdown, daemon=True).start()
        else:
            raise KeyError(action)
        return {"ok": True}


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "fusion-needle/" + __version__
        protocol_version = "HTTP/1.1"

        def _send(self, status: int, data: bytes, kind: str, cache: bool = False):
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "max-age=3600" if cache else "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
                             "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        def _json(self, status: int, payload):
            self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")

        def _host_ok(self) -> bool:
            """Loopback names only: a page on another site cannot reach this through DNS tricks."""
            host = self.headers.get("Host") or ""
            name = host.split("]")[0] + "]" if host.startswith("[") else host.rsplit(":", 1)[0]
            return name in LOOPBACK_HOSTS

        def _authorised(self, query: dict) -> bool:
            given = self.headers.get("X-FN-Token") or (query.get("k") or [""])[0]
            return hmac.compare_digest(given.encode("utf-8"), app.token.encode("utf-8"))

        def do_GET(self):
            parts = urlsplit(self.path)
            path, query = parts.path, parse_qs(parts.query)
            if not self._host_ok():
                self._json(403, {"error": "this app only answers on 127.0.0.1"})
                return
            if path in ("/", "/index.html"):
                page = (paths.UI_DIR / "index.html").read_text(encoding="utf-8")
                self._send(200, page.replace("__FN_TOKEN__", app.token).encode("utf-8"), "text/html; charset=utf-8")
                return
            if path.startswith("/ui/"):
                target = (paths.UI_DIR / path[4:]).resolve()
                if paths.UI_DIR.resolve() not in target.parents or not target.is_file():
                    self._json(404, {"error": "not found"})
                    return
                kind = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
                if kind.startswith("text/") or kind.endswith(("javascript", "json")):
                    kind += "; charset=utf-8"
                self._send(200, target.read_bytes(), kind)
                return
            if not path.startswith("/api/"):
                self._json(404, {"error": "not found"})
                return
            if not self._authorised(query):
                self._json(401, {"error": "reload the page: this window belongs to an earlier launch"})
                return
            try:
                if path == "/api/state":
                    self._json(200, app.state())
                elif path == "/api/tools":
                    self._json(200, app.tools())
                elif path == "/api/help":
                    self._json(200, app.help())
                elif path == "/api/diagnostics":
                    self._json(200, app.diagnostics.summary())
                elif path.startswith("/api/image/"):
                    image = app.session.images.get(path.rsplit("/", 1)[1])
                    if image is None:
                        self._json(404, {"error": "image no longer kept"})
                        return
                    mime = image.get("mime", "image/png")
                    if mime not in ("image/png", "image/jpeg", "image/gif", "image/webp", "image/bmp"):
                        mime = "application/octet-stream"
                    try:
                        data = base64.b64decode(image["data"], validate=False)
                    except (binascii.Error, ValueError):
                        self._json(422, {"error": "image data is not base64"})
                        return
                    self._send(200, data, mime, cache=True)
                elif path == "/api/script":
                    index = int((query.get("step") or ["-1"])[0])
                    call = int((query.get("call") or ["-1"])[0])
                    self._json(200, app.session.script(index, call))
                else:
                    self._json(404, {"error": "not found"})
            except ActionError as failure:
                self._json(400, {"error": str(failure)})
            except ValueError:
                self._json(400, {"error": "bad request"})

        def do_POST(self):
            parts = urlsplit(self.path)
            if not self._host_ok():
                self._json(403, {"error": "this app only answers on 127.0.0.1"})
                return
            if not parts.path.startswith("/api/"):
                self._json(404, {"error": "not found"})
                return
            if not self._authorised({}):
                self._json(401, {"error": "reload the page: this window belongs to an earlier launch"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = -1
            if not 0 <= length <= MAX_BODY:
                self.close_connection = True
                self._json(413, {"error": "body too large"})
                return
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(body, dict):
                    raise ValueError
            except (ValueError, UnicodeDecodeError):
                self._json(400, {"error": "body is not a JSON object"})
                return
            try:
                self._json(200, app.dispatch(parts.path[5:], body))
            except Busy as failure:
                self._json(409, {"error": str(failure)})
            except ActionError as failure:
                self._json(400, {"error": str(failure)})
            except KeyError:
                self._json(404, {"error": "unknown action"})
            except Exception as failure:                 # keep the UI alive whatever an action does
                self._json(500, {"error": f"{type(failure).__name__}: {failure}"})

        do_HEAD = do_GET

        def log_message(self, fmt, *args):
            pass

    return Handler


def serve(app: App, port: int = 0) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app))
    httpd.daemon_threads = True
    app.httpd = httpd
    threading.Thread(target=httpd.serve_forever, name="ui-http", daemon=True).start()
    return httpd
