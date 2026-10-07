"""A stand-in for the interpreter that runs the step server, for the process-lifetime tests.

Started the way the app starts the model server (``<this> -m stepserver.cli serve --port N ...``),
it answers /health and /v1/tools on that port and starts one "engine worker" child
that ignores everything (no stdin, SIGTERM and SIGINT ignored where the OS allows),
so only a kill of the whole group or tree removes it. The pids are written as JSON
to the file named by FAKE_SERVE_PIDS. Standard library only.
"""
import json
import os
import signal
import subprocess
import sys
import threading
import socketserver
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class LocalHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer without the reverse DNS lookup of its own address at start-up
    (HTTPServer.server_bind calls socket.getfqdn, which can take many seconds on macOS)."""

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


WORKER = ("import signal, time\n"
          "for name in ('SIGTERM', 'SIGINT', 'SIGHUP'):\n"
          "    if hasattr(signal, name):\n"
          "        try: signal.signal(getattr(signal, name), signal.SIG_IGN)\n"
          "        except (ValueError, OSError): pass\n"
          "time.sleep(600)\n")
TOOLS = [{"name": "create_sketch", "description": "Start a sketch.", "parameters": {"type": "object", "properties": {}}}]


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            body = {"status": "ok", "project": "fusion-mcp", "model": "fake-serve.cact", "tuned": True,
                    "needle_version": "3.1.1", "tools": len(TOOLS), "max_tools_per_step": 5}
        elif self.path == "/v1/tools":
            body = {"project": "fusion-mcp", "max_tools_per_step": 5, "tools": TOOLS}
        else:
            body = {"error": "not found"}
        data = json.dumps(body).encode()
        self.send_response(200 if "error" not in body else 404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_args):
        pass


def main() -> int:
    argv = sys.argv[1:]
    port = int(argv[argv.index("--port") + 1])
    worker = subprocess.Popen([sys.executable, "-c", WORKER], stdin=subprocess.DEVNULL)
    httpd = LocalHTTPServer(("127.0.0.1", port), Handler)
    httpd.daemon_threads = True
    target = os.environ.get("FAKE_SERVE_PIDS")
    if target:
        with open(target + ".tmp", "w", encoding="utf-8") as handle:
            json.dump({"server": os.getpid(), "worker": worker.pid, "argv": argv,
                       "token": bool(os.environ.get("STEPSERVER_TOKEN"))}, handle)
        os.replace(target + ".tmp", target)
    print(f"fake serve on {port}", flush=True)
    if os.environ.get("FAKE_SERVE_STUBBORN") == "1":        # does not even stop on Ctrl+C
        signal.signal(signal.SIGINT, signal.SIG_IGN)
    stop = threading.Event()
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        stop.wait()
    except KeyboardInterrupt:
        print("fake serve: interrupted", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
