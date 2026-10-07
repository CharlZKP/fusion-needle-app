"""A stand-in for Autodesk's Fusion MCP server, for tests and for trying the UI
without Fusion. Standard library only.

    python fake_fusion.py --port 27182        # then point the app at http://127.0.0.1:27182/mcp

It mimics what the recorded session and the reference client show:

* ``initialize`` answers with serverInfo "MCP Server Adapter" 1.0.0, protocol
  2025-11-25 and an ``MCP-Session-Id`` response header;
* ``notifications/initialized`` -> HTTP 202, empty body;
* any other request without the header -> HTTP 400 ``{"error": "Missing MCP-Session-Id header"}``;
* unknown methods -> JSON-RPC error -32601 ``Method '<m>' not found``;
* ``tools/call`` results are ``{"content": [{"type": "text", "text": "<JSON>"}]}``:
  a script -> ``{"message": "<printed>", "success": true}``; undo / redo ->
  ``{"success": true, ...}`` or ``{"success": false, "error": "Nothing to undo"}``;
  activeCommand idle -> ``{"commandId": "SelectCommand", "commandName": "Select",
  "isDefaultCommand": true}``; screenshot -> an image block (or the JSON-text form).

It keeps a toy design (sketch / bodies / last feature / undo stack) driven by the
``# <tool>: rendered by`` and ``ARGS = {...}`` header of the scripts it receives,
and records every script and call. It does not run the scripts.
"""
from __future__ import annotations

import argparse
import ast
import base64
import copy
import json
import re
import struct
import threading
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PNG_1X1 = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4z8DwHwAFAAH/"
           "q842iQAAAABJRU5ErkJggg==")
FEATURE_NAMES = {"extrude": "Extrude", "create_hole": "Hole", "fillet": "Fillet", "chamfer": "Chamfer",
                 "shell": "Shell", "circular_pattern": "CircularPattern",
                 "rectangular_pattern": "RectangularPattern", "mirror": "Mirror", "revolve": "Revolve",
                 "combine": "Combine", "move_body": "Move"}
HEADER = re.compile(r"^# (\w+): rendered by", re.MULTILINE)
ARGS = re.compile(r"^ARGS = (\{.*\})$", re.MULTILINE)


def png(width: int, height: int) -> str:
    """A grey RGB PNG of that size, base64 (what a screenshot of the asked size looks like)."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    rows = (b"\x00" + b"\x80\x80\x80" * width) * height
    data = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows, 9)) + chunk(b"IEND", b""))
    return base64.b64encode(data).decode("ascii")


class FakeFusion:
    def __init__(self, port: int = 0, sse: bool = False, image_blocks: bool = True):
        self.sse = sse
        self.image_blocks = image_blocks
        self.lock = threading.Lock()
        self.sessions: set[str] = set()
        self._session_seq = 1000
        self.requests: list[dict] = []          # every JSON-RPC message received
        self.calls: list[dict] = []             # every tools/call params
        self.scripts: list[dict] = []           # {"tool", "args", "read_only", "script"} per script
        self.undos = 0
        self.fail_tools: dict[str, str] = {}    # tool name -> error text (the script "raises")
        self.fail_style = "isError"             # isError | success_false | jsonrpc
        self.dialog: str | None = None          # name of an open command dialog
        self.active_command_answer = None       # a dict (sent as JSON text) or a str: replaces the activeCommand answer
        self.orientation = {"default_modeling_orientation": "ZUpModelingOrientation", "front_up": [0.0, 0.0, 1.0],
                            "front_eye": [0.0, -1.0, 0.0], "camera_up": [0.0, 0.0, 1.0], "document": "Untitled",
                            "is_design": True, "errors": {}}
        self.ignore_image_size = False          # as if the server did not apply width / height
        self.documents = [{"name": "Bracket v3", "id": "urn:adsk.wipprod:dm.lineage:AAA"},
                          {"name": "Bracket v4", "id": "urn:adsk.wipprod:dm.lineage:BBB"}]
        self.opened: list[str] = []
        self.design = {"sketch": None, "bodies": [], "last_feature": None, "counts": {}}
        self.history: list[dict] = []
        self.redo_stack: list[dict] = []
        handler = self._handler()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}/mcp"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def drop_sessions(self):
        """As if Fusion had been restarted."""
        self.sessions.clear()

    # ---- the toy design ------------------------------------------------
    def _next(self, prefix: str) -> str:
        counts = self.design["counts"]
        counts[prefix] = counts.get(prefix, 0) + 1
        return f"{prefix}{counts[prefix]}"

    def modelling_scripts(self) -> list[dict]:
        return [entry for entry in self.scripts if not entry["read_only"]]

    def _apply(self, tool: str, args: dict) -> dict:
        design = self.design
        report = {"tool": tool, "feature": None, "bodies": design["bodies"]}
        if tool == "create_sketch":
            where = f"{args['face']} face" if args.get("face") else args.get("plane", "xy")
            design["sketch"] = {"name": self._next("Sketch"), "on": where}
            report["feature"] = design["sketch"]["name"]
        elif tool.startswith("draw_") or tool in ("add_dimension", "add_constraint"):
            if design["sketch"] is None:
                raise RuntimeError("no active sketch")
            report["feature"] = design["sketch"]["name"]
        elif tool in FEATURE_NAMES:
            if tool == "extrude":
                if design["sketch"] is None:
                    raise RuntimeError("no sketch to extrude")
                if args.get("operation", "new_body") == "new_body" or not design["bodies"]:
                    design["bodies"] = design["bodies"] + [f"Body{len(design['bodies']) + 1}"]
                design["sketch"] = None
            elif not design["bodies"]:
                raise RuntimeError("the design has no body")
            design["last_feature"] = self._next(FEATURE_NAMES[tool])
            report["feature"] = design["last_feature"]
        report["bodies"] = design["bodies"]
        return report

    def _state_line(self) -> str:
        design = self.design
        return json.dumps({"state": 1, "units": "mm", "sketch": design["sketch"], "bodies": design["bodies"],
                           "last_feature": design["last_feature"], "unhealthy": []})

    # ---- tools ---------------------------------------------------------
    @staticmethod
    def _text(payload: dict, is_error: bool = False) -> dict:
        result = {"content": [{"type": "text", "text": json.dumps(payload)}]}
        if is_error:
            result["isError"] = True
        return result

    def _failure(self, message: str):
        if self.fail_style == "jsonrpc":
            raise RpcError(-32000, message)
        if self.fail_style == "success_false":
            return self._text({"success": False, "error": message})
        return {"content": [{"type": "text", "text": message}], "isError": True}

    def _call(self, name: str, arguments: dict) -> dict:
        if name == "fusion_mcp_execute":
            kind, body = arguments.get("featureType"), arguments.get("object") or {}
            if kind == "document":
                if body.get("operation") == "open":
                    self.opened.append(body.get("fileId"))
                    self.design = {"sketch": None, "bodies": [], "last_feature": None, "counts": {}}
                    self.history.clear()
                return self._text({"success": True, "operation": body.get("operation")})
            if kind != "script":
                return self._failure(f"unsupported featureType {kind!r}")
            script = body.get("script", "")
            read_only = bool(body.get("readOnly"))
            header = HEADER.search(script)
            tool = header.group(1) if header else ("state" if '"state": 1' in script else
                                                   "orientation" if '"probe": "orientation"' in script else "script")
            found = ARGS.search(script)
            args = ast.literal_eval(found.group(1)) if found else {}
            self.scripts.append({"tool": tool, "args": args, "read_only": read_only, "script": script})
            if tool == "state":
                return self._text({"message": self._state_line() + "\n", "success": True})
            compile(script, f"<{tool}>", "exec")           # a rendered script must at least be Python
            if tool == "orientation" and "orientation" not in self.fail_tools:
                return self._text({"message": json.dumps({"probe": "orientation", **self.orientation}) + "\n",
                                   "success": True})
            if tool in self.fail_tools:
                return self._failure(self.fail_tools[tool])
            if read_only:
                return self._text({"message": json.dumps({"tool": tool, "bodies": self.design["bodies"]}) + "\n",
                                   "success": True})
            if self.dialog:
                return self._failure(f"Cannot run a script while the '{self.dialog}' command is active")
            before = copy.deepcopy(self.design)
            try:
                report = self._apply(tool, args)
            except RuntimeError as failure:
                self.design = before
                return self._failure(f"Traceback (most recent call last):\n  ...\nRuntimeError: {failure}")
            self.history.append(before)
            self.redo_stack.clear()
            return self._text({"message": json.dumps(report) + "\n", "success": True})
        if name == "fusion_mcp_update":
            kind = arguments.get("featureType")
            if kind == "undo":
                self.undos += 1
                if not self.history:
                    return self._text({"success": False, "error": "Nothing to undo", "canUndo": False})
                self.redo_stack.append(copy.deepcopy(self.design))
                self.design = self.history.pop()
                return self._text({"success": True, "message": "Undo", "canUndo": bool(self.history),
                                   "canRedo": True})
            if kind == "redo":
                if not self.redo_stack:
                    return self._text({"success": False, "error": "Nothing to redo", "canRedo": False})
                self.history.append(copy.deepcopy(self.design))
                self.design = self.redo_stack.pop()
                return self._text({"success": True, "message": "Redo", "canUndo": True,
                                   "canRedo": bool(self.redo_stack)})
            return self._failure(f"unknown featureType {kind!r}")
        if name == "fusion_mcp_read":
            query = arguments.get("queryType")
            if query == "activeCommand":
                if isinstance(self.active_command_answer, str):
                    return {"content": [{"type": "text", "text": self.active_command_answer}]}
                if self.active_command_answer is not None:
                    return self._text(self.active_command_answer)
                if self.dialog:
                    return self._text({"commandId": self.dialog + "Command", "commandName": self.dialog,
                                       "isDefaultCommand": False, "inputs": []})
                return self._text({"commandId": "SelectCommand", "commandName": "Select",
                                   "isDefaultCommand": True})
            if query == "screenshot":
                sized = "width" in arguments and "height" in arguments and not self.ignore_image_size
                data = png(int(arguments["width"]), int(arguments["height"])) if sized else PNG_1X1
                if self.image_blocks:
                    return {"content": [{"type": "image", "data": data, "mimeType": "image/png"}]}
                return self._text({"type": "image", "mimeType": "image/png", "base64Data": data})
            if query == "document":
                wanted = str(arguments.get("name", "")).lower()
                if arguments.get("operation") == "search":
                    return self._text({"results": [d for d in self.documents if wanted in d["name"].lower()],
                                       "success": True})
                return self._text({"results": [{"name": "Untitled", "id": "", "isActive": True,
                                                "isModified": bool(self.history)}], "success": True})
            return self._failure(f"unknown queryType {query!r}")
        raise RpcError(-32602, f"Unknown tool: {name}")

    # ---- JSON-RPC ------------------------------------------------------
    def _rpc(self, message: dict):
        method = message.get("method")
        if method == "tools/list":
            return {"tools": [{"name": name, "description": "", "inputSchema": {"type": "object"}}
                              for name in ("fusion_mcp_electronics_read", "fusion_mcp_execute",
                                           "fusion_mcp_read", "fusion_mcp_update")]}
        if method == "resources/list":
            return {"resources": []}
        if method == "tools/call":
            params = message.get("params") or {}
            self.calls.append(params)
            return self._call(params.get("name"), params.get("arguments") or {})
        raise RpcError(-32601, f"Method '{method}' not found")

    def _handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _send(self, status, body=b"", kind="application/json", headers=None):
                self.send_response(status)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(body)))
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                if self.path.split("?")[0] != "/mcp":
                    self._send(404, b'{"error": "not found"}')
                    return
                length = int(self.headers.get("Content-Length", "0"))
                try:
                    message = json.loads(self.rfile.read(length))
                except ValueError:
                    self._send(400, b'{"error": "Invalid JSON"}')
                    return
                with fake.lock:
                    fake.requests.append(message)
                    method = message.get("method")
                    if method == "initialize":
                        fake._session_seq += 1
                        session = str(fake._session_seq)
                        fake.sessions.add(session)
                        result = {"capabilities": {"resources": {"listChanged": False, "subscribe": False},
                                                   "tools": {"listChanged": False}},
                                  "protocolVersion": "2025-11-25",
                                  "serverInfo": {"name": "MCP Server Adapter", "version": "1.0.0"}}
                        body = json.dumps({"jsonrpc": "2.0", "id": message.get("id"), "result": result})
                        self._send(200, body.encode(), headers={"MCP-Session-Id": session})
                        return
                    session = self.headers.get("MCP-Session-Id")
                    if not session:
                        self._send(400, b'{"error": "Missing MCP-Session-Id header"}')
                        return
                    if session not in fake.sessions:
                        self._send(404, b'{"error": "Unknown session"}')
                        return
                    if "id" not in message:                 # a notification
                        self._send(202)
                        return
                    try:
                        answer = {"jsonrpc": "2.0", "id": message["id"], "result": fake._rpc(message)}
                    except RpcError as failure:
                        answer = {"jsonrpc": "2.0", "id": message["id"],
                                  "error": {"code": failure.code, "message": str(failure)}}
                if fake.sse:
                    body = f"event: message\ndata: {json.dumps(answer)}\n\n".encode()
                    self._send(200, body, kind="text/event-stream")
                else:
                    self._send(200, json.dumps(answer).encode())

            def do_GET(self):
                self._send(405, b'{"error": "Method not allowed"}')

            def log_message(self, *_args):
                pass

        return Handler


class RpcError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fake Fusion MCP server (for trying the app without Fusion)")
    parser.add_argument("--port", type=int, default=27182)
    parser.add_argument("--sse", action="store_true", help="answer as text/event-stream")
    options = parser.parse_args()
    server = FakeFusion(options.port, sse=options.sse)
    print(f"fake Fusion MCP at {server.url} (Ctrl+C to stop)", flush=True)
    try:
        server.httpd.serve_forever()
    except KeyboardInterrupt:
        pass
