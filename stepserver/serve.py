"""The JSON API the desktop app calls. Standard library only.

    GET  /health            liveness, model and project info (no auth)
    GET  /v1/tools          the project's tool catalogue
    POST /v1/step           {"query", "system"?, "tools": [names]?} -> calls

One request runs at a time; the engine is not thread-safe and a step is short.
"""

from __future__ import annotations

import hmac
import json
import os
import sys
import threading
import time
import socketserver
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .calls import fold_numbers, normalise_calls
from .engine import BASE, EngineClient, EngineError


class LocalHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer without the reverse DNS lookup of its own address at start-up
    (HTTPServer.server_bind calls socket.getfqdn, which can take many seconds on macOS)."""

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


MAX_BODY = 1 << 20


class StepError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class StepService:
    """Protocol logic, separate from HTTP so it can be tested with a fake engine."""

    def __init__(self, project, client, weights: str, max_new_tokens: int = 512):
        self.project = project
        self.client = client
        self.weights = weights
        self.max_new_tokens = max_new_tokens
        self.catalogue = project.catalogue
        self.by_name = project.tools_by_name
        self.started = time.time()
        self.steps = 0
        self._lock = threading.Lock()

    def health(self) -> dict:
        return {
            "status": "ok" if self.client.alive() else "engine-down",
            "project": self.project.name,
            "model": Path(self.weights).name if self.weights != BASE else BASE,
            "tuned": self.weights != BASE,
            "needle_version": self.client.info.get("needle_version"),
            "tools": len(self.catalogue),
            "max_tools_per_step": self.project.max_tools,
            "steps_served": self.steps,
            "uptime_s": round(time.time() - self.started, 1),
        }

    def tools(self) -> dict:
        return {"project": self.project.name, "max_tools_per_step": self.project.max_tools,
                "tools": self.catalogue}

    def step(self, body) -> dict:
        if not isinstance(body, dict):
            raise StepError(400, "body must be a JSON object")
        query = body.get("query")
        if not isinstance(query, str) or not query.strip():
            raise StepError(400, "'query' must be a non-empty string")
        system = body.get("system") or ""
        if not isinstance(system, str):
            raise StepError(400, "'system' must be a string of facts")
        names = body.get("tools")
        if names is None:
            tools, toolset = self.catalogue, "catalogue"
        else:
            if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
                raise StepError(400, "'tools' must be a list of tool names from GET /v1/tools")
            unknown = [name for name in names if name not in self.by_name]
            if unknown:
                raise StepError(400, f"unknown tools: {unknown}")
            if len(set(names)) != len(names):
                raise StepError(400, "'tools' lists a name twice")
            if not names:
                raise StepError(400, "'tools' is empty; omit it to use the whole catalogue")
            tools, toolset = [self.by_name[name] for name in names], "request"
        started = time.perf_counter()
        with self._lock:
            try:
                reply = self.client.step(query, system, tools, self.max_new_tokens)
            except EngineError as failure:
                raise StepError(503, f"engine error: {failure}") from failure
            self.steps += 1
        response = reply["response"]
        out = {
            # integer-valued floats are folded (6.0 -> 6) so the calls read like the request
            "calls": fold_numbers(normalise_calls(response.get("function_calls"))),
            "suppressed": fold_numbers(normalise_calls(response.get("suppressed_calls"))),
            "reasoning": response.get("reasoning") or "",
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
            "toolset": toolset,
            "retrieval": len(tools) > 5,
            "agent_cache": reply.get("cache"),
        }
        if response.get("validation"):
            out["validation"] = response["validation"]
        if response.get("success") is False or response.get("error"):
            # e.g. "tool call truncated: token budget exhausted": treat like an empty answer
            out["engine_error"] = response.get("error") or "engine reported failure"
        return out


def make_handler(service: StepService, token: str | None, cors_origin: str | None, quiet: bool):
    class Handler(BaseHTTPRequestHandler):
        server_version = "stepserver/0.1"
        protocol_version = "HTTP/1.1"

        def _send(self, status: int, payload: dict):
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            if cors_origin:
                self.send_header("Access-Control-Allow-Origin", cors_origin)
                self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.end_headers()
            self.wfile.write(data)

        def _authorised(self) -> bool:
            if not token:
                return True
            header = self.headers.get("Authorization", "")
            return hmac.compare_digest(header.encode("utf-8"), f"Bearer {token}".encode("utf-8"))

        def do_OPTIONS(self):
            self._send(204 if cors_origin else 405, {})

        def do_GET(self):
            path = self.path.split("?", 1)[0].rstrip("/") or "/"
            if path == "/health":
                self._send(200, service.health())
            elif path == "/v1/tools":
                if not self._authorised():
                    self._send(401, {"error": "missing or wrong bearer token"})
                else:
                    self._send(200, service.tools())
            else:
                self._send(404, {"error": "not found", "endpoints": ["GET /health", "GET /v1/tools", "POST /v1/step"]})

        def do_POST(self):
            path = self.path.split("?", 1)[0].rstrip("/")
            if path != "/v1/step":
                self._send(404, {"error": "not found"})
                return
            if not self._authorised():
                self._send(401, {"error": "missing or wrong bearer token"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = -1
            if length < 0 or length > MAX_BODY:
                self.close_connection = True
                self._send(413, {"error": f"body must be 0..{MAX_BODY} bytes with a Content-Length"})
                return
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._send(400, {"error": "body is not valid JSON"})
                return
            try:
                self._send(200, service.step(body))
            except StepError as failure:
                self._send(failure.status, {"error": str(failure)})
            except Exception as failure:  # keep the server up whatever a request does
                self._send(500, {"error": f"{type(failure).__name__}: {failure}"})

        def log_message(self, fmt, *args):
            if not quiet:
                sys.stderr.write("serve: %s %s\n" % (self.address_string(), fmt % args))

    return Handler


def serve(project, weights: str, host: str = "127.0.0.1", port: int = 8765, agents: int = 4,
          cors_origin: str | None = None, quiet: bool = False, warm: bool = True,
          ready_event: threading.Event | None = None, max_new_tokens: int = 512) -> int:
    token = os.environ.get("STEPSERVER_TOKEN") or None
    if host not in ("127.0.0.1", "localhost", "::1") and not token:
        print(f"serve: WARNING: binding to {host} without STEPSERVER_TOKEN: anyone who can reach this port "
              "can call the model", file=sys.stderr)
    print(f"serve: loading {weights} for project {project.name} ...", file=sys.stderr, flush=True)
    client = EngineClient(weights, agents=agents, index_dir=project.cache_dir)
    service = StepService(project, client, weights, max_new_tokens=max_new_tokens)
    try:
        if warm:
            # Fetches the engine on first use and loads the weights, so the first
            # real request does not pay for that.
            started = time.time()
            service.client.step("warm up", "", service.catalogue, 8)
            print(f"serve: engine warm in {time.time() - started:.1f} s", file=sys.stderr, flush=True)
        server = LocalHTTPServer((host, port), make_handler(service, token, cors_origin, quiet))
        server.daemon_threads = True
        print(f"serve: http://{host}:{server.server_address[1]}  model {service.health()['model']}  "
              f"auth {'bearer token' if token else 'none (localhost)'}", file=sys.stderr, flush=True)
        if ready_event is not None:
            ready_event.set()
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
    finally:
        client.close()
    return 0
