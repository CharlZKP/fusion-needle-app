"""Running Needle through the shipped engine, one worker process per weights.

Why a worker process:

- A tuned archive binds to the process that loads it. Keeping every model in its
  own child means a crashed engine never takes the HTTP server down.
- One `needle.Needle` instance is one toolset plus one system turn; neither can
  be changed afterwards. The step protocol changes both on every turn (the state
  facts move, the five tools are reshuffled), so the worker keeps a small LRU of
  agents keyed by (tools, system) and builds a new one on a miss.
- Every agent is created with `stateless=True`, so each step starts from a clean
  conversation: a step never sees the previous step's query.

Costs, as measured on needle 3.1.1:

- base weights: all agents share the one in-process engine; switching agent
  re-runs `needle_init`, i.e. re-encodes the system + tools prefix.
- tuned weights: every agent is its own grandchild process with its own copy of
  the weights and its own prefix cache. A hit is free, a miss pays process start
  + weight load + prefix encoding, and each cached agent holds memory, hence the
  small default LRU.

- more than 5 tools (the whole catalogue): Needle embeds every schema when the
  agent is built and retrieves 5 per query. `tool_index_path` is passed so a
  future engine can persist those embeddings, but engine 3.1.0 wrote no index
  file in our tests: every new (catalogue, system) context re-embeds the
  catalogue. Sending at most 5 tool names avoids that cost entirely.

The protocol is one JSON object per line on stdin/stdout.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from collections import OrderedDict
from pathlib import Path

BASE = "base"
DEFAULT_AGENTS = 4
RETRIEVAL_LIMIT = 5  # above this many tools Needle retrieves the top 5 itself


def agent_key(tools: list[dict], system: str) -> str:
    payload = json.dumps([tools, system or ""], sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class AgentPool:
    """LRU of needle.Needle agents for one set of weights. Lives inside the worker process."""

    def __init__(self, weights: str | None, max_agents: int = DEFAULT_AGENTS, index_dir: str | None = None):
        import needle  # deferred: the parent process never loads the engine

        self._needle = needle
        self.weights = None if weights in (None, "", BASE) else str(weights)
        self.max_agents = max(1, int(max_agents))
        self.index_dir = index_dir
        self.agents: OrderedDict[str, object] = OrderedDict()
        self.hits = 0
        self.misses = 0

    def _index_path(self, tools: list[dict]) -> str | None:
        """Persist tool embeddings for catalogues large enough to trigger retrieval."""
        if not self.index_dir or len(tools) <= RETRIEVAL_LIMIT:
            return None
        os.makedirs(self.index_dir, exist_ok=True)
        tag = hashlib.sha256((self.weights or BASE).encode("utf-8")).hexdigest()[:12]
        return os.path.join(self.index_dir, f"tools-{tag}.idx")

    def _agent(self, tools: list[dict], system: str):
        key = agent_key(tools, system)
        agent = self.agents.get(key)
        if agent is not None:
            self.agents.move_to_end(key)
            self.hits += 1
            return agent, True
        self.misses += 1
        while len(self.agents) >= self.max_agents:
            _old_key, old = self.agents.popitem(last=False)
            try:
                old.close()
            except Exception:
                pass
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # the "no confidence head" notice, once per agent
            agent = self._needle.Needle(
                tools=tools, system=system or None, weights=self.weights,
                tool_index_path=self._index_path(tools), auto_date=False, stateless=True)
        self.agents[key] = agent
        return agent, False

    def step(self, query: str, system: str, tools: list[dict], max_new_tokens: int = 512) -> dict:
        started = time.perf_counter()
        agent, hit = self._agent(tools, system or "")
        ready = time.perf_counter()
        response = agent.complete(query, max_new_tokens=max_new_tokens)
        done = time.perf_counter()
        return {
            "response": response,
            "cache": "hit" if hit else "miss",
            "init_ms": round((ready - started) * 1000, 1),
            "complete_ms": round((done - ready) * 1000, 1),
            "latency_ms": round((done - started) * 1000, 1),
        }

    def close(self):
        for agent in self.agents.values():
            try:
                agent.close()
            except Exception:
                pass
        self.agents.clear()


def worker_main(argv: list[str] | None = None) -> int:
    """Entry point of the child: `python -m stepserver.engine --weights W [--agents N] [--index-dir D]`."""
    import argparse

    parser = argparse.ArgumentParser(prog="stepserver.engine")
    parser.add_argument("--weights", default=BASE)
    parser.add_argument("--agents", type=int, default=DEFAULT_AGENTS)
    parser.add_argument("--index-dir", default=None)
    args = parser.parse_args(argv)

    # Keep the protocol channel private: anything the engine or needle prints to
    # stdout goes to stderr instead.
    channel = os.fdopen(os.dup(sys.stdout.fileno()), "w", encoding="utf-8", buffering=1)
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    sys.stdout = sys.stderr

    def send(message: dict):
        channel.write(json.dumps(message, ensure_ascii=False) + "\n")
        channel.flush()

    try:
        import needle
        pool = AgentPool(args.weights, args.agents, args.index_dir)
        send({"ok": True, "ready": True, "needle_version": getattr(needle, "__version__", None),
              "weights": pool.weights or BASE, "pid": os.getpid()})
    except Exception as failure:
        send({"ok": False, "fatal": True, "error": f"{type(failure).__name__}: {failure}"})
        return 1

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
            operation = request.get("op")
            if operation == "step":
                result = pool.step(request["query"], request.get("system") or "", request["tools"],
                                   int(request.get("max_new_tokens", 512)))
                send({"ok": True, **result})
            elif operation == "stats":
                send({"ok": True, "hits": pool.hits, "misses": pool.misses, "agents": len(pool.agents)})
            elif operation == "close":
                send({"ok": True})
                break
            else:
                send({"ok": False, "error": f"unknown op {operation!r}"})
        except Exception as failure:
            send({"ok": False, "error": f"{type(failure).__name__}: {failure}"})
    pool.close()
    return 0


class EngineError(RuntimeError):
    pass


class EngineClient:
    """Parent-side handle on one worker process. Thread-safe: one request at a time."""

    def __init__(self, weights: str | os.PathLike | None = BASE, agents: int = DEFAULT_AGENTS,
                 index_dir: str | os.PathLike | None = None, startup_timeout: float = 900.0):
        self.weights = BASE if weights in (None, "", BASE) else str(Path(weights).resolve())
        if self.weights != BASE:
            if not self.weights.endswith(".cact"):
                raise EngineError(f"--weights expects a .cact archive or 'base', got {weights}")
            if not os.path.isfile(self.weights):
                raise EngineError(f"weights not found: {self.weights}")
        self._lock = threading.Lock()
        command = [sys.executable, "-m", "stepserver.engine", "--weights", self.weights, "--agents", str(agents)]
        if index_dir:
            command += ["--index-dir", str(index_dir)]
        env = dict(os.environ)
        env.setdefault("NEEDLE_TELEMETRY", "0")
        env["PYTHONUNBUFFERED"] = "1"
        self._process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         text=True, encoding="utf-8", bufsize=1, env=env)
        self.info = self._read(startup_timeout)
        if not self.info.get("ok"):
            self.close()
            raise EngineError(self.info.get("error", "engine worker failed to start"))

    def _read(self, timeout: float | None = None) -> dict:
        result: dict = {}

        def read():
            line = self._process.stdout.readline()
            if line:
                try:
                    result.update(json.loads(line))
                except json.JSONDecodeError:
                    result.update({"ok": False, "error": f"bad worker output: {line[:200]!r}"})

        thread = threading.Thread(target=read, daemon=True)
        thread.start()
        thread.join(timeout)
        if thread.is_alive():
            raise EngineError(f"engine worker did not answer within {timeout} s")
        if not result:
            raise EngineError(f"engine worker exited (code {self._process.poll()})")
        return result

    def _request(self, message: dict, timeout: float | None = None) -> dict:
        with self._lock:
            if self._process.poll() is not None:
                raise EngineError(f"engine worker exited (code {self._process.returncode})")
            try:
                self._process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
                self._process.stdin.flush()
            except (BrokenPipeError, OSError) as failure:
                raise EngineError("engine worker exited unexpectedly") from failure
            reply = self._read(timeout)
        if not reply.get("ok"):
            raise EngineError(reply.get("error", "engine worker failed"))
        return reply

    def step(self, query: str, system: str, tools: list[dict], max_new_tokens: int = 512,
             timeout: float | None = None) -> dict:
        return self._request({"op": "step", "query": query, "system": system or "", "tools": tools,
                              "max_new_tokens": max_new_tokens}, timeout)

    def stats(self) -> dict:
        return self._request({"op": "stats"}, 60)

    def alive(self) -> bool:
        return self._process.poll() is None

    def close(self):
        process = self._process
        if process.poll() is None:
            try:
                with self._lock:
                    process.stdin.write('{"op": "close"}\n')
                    process.stdin.flush()
                process.wait(timeout=10)
            except Exception:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
        for stream in (process.stdin, process.stdout):
            try:
                stream.close()
            except Exception:
                pass

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()


if __name__ == "__main__":
    raise SystemExit(worker_main())
