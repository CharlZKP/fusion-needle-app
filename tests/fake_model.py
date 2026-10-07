"""A stand-in for the step server with scripted answers, for fast tests. Standard library only.

Serves /health, /v1/tools and /v1/step with the response shapes of
README.md. ``answers`` maps a query to the step response, to a list of
responses used in turn, or to a function (query, system, names) -> response; unknown queries get an empty answer.
"""
from __future__ import annotations

import json
import threading
import socketserver
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class LocalHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer without the reverse DNS lookup of its own address at start-up
    (HTTPServer.server_bind calls socket.getfqdn, which can take many seconds on macOS)."""

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


class FakeModel:
    def __init__(self, catalogue: list[dict], answers: dict | None = None, token: str = "", port: int = 0):
        self.catalogue = catalogue
        self.answers = dict(answers or {})
        self.token = token
        self.steps: list[dict] = []             # every /v1/step body received
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _send(self, status, payload):
                data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _ok(self):
                return not fake.token or self.headers.get("Authorization") == f"Bearer {fake.token}"

            def do_GET(self):
                if self.path == "/health":
                    self._send(200, {"status": "ok", "project": "fusion-mcp", "model": "fake-20L.cact",
                                     "tuned": True, "needle_version": "3.1.1", "tools": len(fake.catalogue),
                                     "max_tools_per_step": 5, "steps_served": len(fake.steps), "uptime_s": 1.0})
                elif self.path == "/v1/tools":
                    if not self._ok():
                        self._send(401, {"error": "missing or wrong bearer token"})
                    else:
                        self._send(200, {"project": "fusion-mcp", "max_tools_per_step": 5, "tools": fake.catalogue})
                else:
                    self._send(404, {"error": "not found"})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")
                if self.path != "/v1/step":
                    self._send(404, {"error": "not found"})
                elif not self._ok():
                    self._send(401, {"error": "missing or wrong bearer token"})
                else:
                    self._send(200, fake.step(body["query"], body.get("system", ""), body.get("tools")))

            def log_message(self, *_args):
                pass

        self.httpd = LocalHTTPServer(("127.0.0.1", port), Handler)
        self.httpd.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def step(self, query, system, names):
        self.steps.append({"query": query, "system": system, "tools": names})
        answer = self.answers.get(query, {})
        if callable(answer):
            answer = answer(query, system, names)
        if isinstance(answer, list):
            answer = answer.pop(0) if len(answer) > 1 else answer[0]
        out = {"calls": [], "suppressed": [], "reasoning": "", "latency_ms": 1.0,
               "toolset": "request" if names else "catalogue", "retrieval": not names, "agent_cache": "miss"}
        out.update(json.loads(json.dumps(answer)))
        return out
