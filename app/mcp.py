"""Client for Autodesk's Fusion MCP server (streamable HTTP, JSON-RPC 2.0).

Handshake as in the recorded inspector session and the reference client:
``initialize`` -> the server answers with an ``MCP-Session-Id`` header ->
``notifications/initialized`` (202) -> ``tools/call`` with that header on every
request. Answers may come as plain JSON or as one SSE stream; both are read.
Standard library only.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request

from . import __version__

PROTOCOL_VERSION = "2025-11-25"
SESSION_HEADER = "MCP-Session-Id"


class McpError(Exception):
    """Transport or protocol failure (not a tool that reported an error)."""


class McpSessionError(McpError):
    """HTTP 400 / 404: the session id is missing or no longer known. Nothing was executed."""


class McpUnreachable(McpError):
    """Nothing answers at the URL: Fusion is not running or its MCP server is off."""


def _sse_messages(body: str):
    data: list[str] = []
    for line in body.splitlines() + [""]:
        if line.startswith("data:"):
            data.append(line[5:].lstrip(" "))
        elif not line.strip() and data:
            try:
                yield json.loads("\n".join(data))
            except ValueError:
                pass
            data = []


class FusionClient:
    def __init__(self, url: str, timeout: float = 180.0, recorder=None):
        """``recorder(entry)`` gets every exchange, raw (diagnostics.Diagnostics.record_call)."""
        self.url = url
        self.timeout = timeout
        self.recorder = recorder
        self.session_id: str | None = None
        self.server_info: dict = {}
        self.protocol = PROTOCOL_VERSION
        self._next_id = 0
        self._last_http: tuple = (None, "")
        self._lock = threading.RLock()
        # never route 127.0.0.1 through a system proxy
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    # ---- transport -----------------------------------------------------
    def _post(self, payload: dict, timeout: float | None = None):
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if self.session_id:
            headers[SESSION_HEADER] = str(self.session_id)
            headers["MCP-Protocol-Version"] = self.protocol
        request = urllib.request.Request(self.url, data=json.dumps(payload).encode("utf-8"),
                                         headers=headers, method="POST")
        try:
            with self._opener.open(request, timeout=timeout or self.timeout) as response:
                session = response.headers.get(SESSION_HEADER)
                if session:
                    self.session_id = session
                kind = response.headers.get("Content-Type", "")
                return response.status, kind, response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as failure:
            body = failure.read().decode("utf-8", "replace")
            return failure.code, failure.headers.get("Content-Type", ""), body
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as failure:
            reason = getattr(failure, "reason", failure)
            raise McpUnreachable(f"no answer from {self.url}: {reason}") from failure

    def _record(self, method: str, params, started: float, status=None, kind: str = "", answer=None,
                body: str | None = None, error: str = ""):
        if self.recorder is None or (method == "ping" and not error):
            return                                     # the keep-alive would push everything else out
        entry = {"method": method, "request": params, "http_status": status, "content_type": kind,
                 "ms": round((time.perf_counter() - started) * 1000, 1), "error": error}
        if answer is not None:
            entry["response"] = answer
        elif body is not None:
            entry["response_text"] = body[:4000]
        try:
            self.recorder(entry)
        except Exception:                              # diagnostics must never break a call
            pass

    def _request(self, method: str, params: dict | None = None, timeout: float | None = None) -> dict:
        started = time.perf_counter()
        status, kind, body = None, "", None
        try:
            answer = self._exchange(method, params, timeout)
        except McpError as failure:
            status, kind, body = getattr(failure, "http", (None, "", None))
            self._record(method, params, started, status, kind, body=body, error=f"{type(failure).__name__}: {failure}")
            raise
        status, kind = self._last_http
        self._record(method, params, started, status, kind, answer=answer)
        return answer

    def _fail(self, error: type, message: str, status, kind: str, body: str):
        failure = error(message)
        failure.http = (status, kind, body)
        return failure

    def _exchange(self, method: str, params: dict | None, timeout: float | None) -> dict:
        self._next_id += 1
        request_id = self._next_id
        payload = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            payload["params"] = params
        status, kind, body = self._post(payload, timeout)
        self._last_http = (status, kind)
        if status in (400, 404):
            raise self._fail(McpSessionError, f"HTTP {status} for {method}: {body[:300]}", status, kind, body)
        if status >= 400:
            raise self._fail(McpError, f"HTTP {status} for {method}: {body[:300]}", status, kind, body)
        messages = list(_sse_messages(body)) if "text/event-stream" in kind else None
        if messages is None:
            try:
                messages = [json.loads(body)] if body.strip() else []
            except ValueError as failure:
                raise self._fail(McpError, f"{method}: answer is not JSON: {body[:200]!r}", status, kind,
                                 body) from failure
        for message in messages:
            if isinstance(message, dict) and message.get("id") == request_id:
                return message
        for message in messages:                       # servers that do not echo the id type
            if isinstance(message, dict) and ("result" in message or "error" in message):
                return message
        raise self._fail(McpError, f"{method}: no JSON-RPC answer in the response", status, kind, body)

    def _notify(self, method: str):
        started = time.perf_counter()
        status, kind, body = self._post({"jsonrpc": "2.0", "method": method})
        problem = f"HTTP {status} for {method}: {body[:300]}" if status >= 400 else ""
        self._record(method, None, started, status, kind, body=body, error=problem)
        if problem:
            raise McpError(problem)

    # ---- protocol ------------------------------------------------------
    def connect(self) -> dict:
        with self._lock:
            self.session_id = None
            answer = self._request("initialize", {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "fusion-needle", "version": __version__},
            }, timeout=15)
            if "error" in answer:
                raise McpError(f"initialize refused: {answer['error']}")
            result = answer.get("result") or {}
            self.server_info = result.get("serverInfo") or {}
            self.protocol = result.get("protocolVersion") or PROTOCOL_VERSION
            self._notify("notifications/initialized")
            return result

    @property
    def connected(self) -> bool:
        return self.session_id is not None

    def disconnect(self):
        self.session_id = None

    def _with_session(self, method: str, params: dict | None, timeout: float | None = None) -> dict:
        with self._lock:
            if not self.session_id:
                self.connect()
            try:
                return self._request(method, params, timeout)
            except McpUnreachable:
                self.session_id = None
                raise
            except McpSessionError:
                # the session has gone (Fusion restarted): one fresh handshake, one retry
                self.connect()
                return self._request(method, params, timeout)

    def alive(self) -> bool:
        """Any JSON-RPC answer proves the server is there (it need not implement ping)."""
        try:
            self._with_session("ping", None, timeout=10)
            return True
        except McpError:
            self.session_id = None
            return False

    def request(self, method: str, params: dict | None = None, timeout: float | None = None) -> dict:
        """Any JSON-RPC request inside the session. -> the raw answer (``result`` or ``error``)."""
        return self._with_session(method, params, timeout)

    def list_tools(self) -> list[dict]:
        answer = self._with_session("tools/list", {})
        if "error" in answer:
            raise McpError(f"tools/list: {answer['error']}")
        return (answer.get("result") or {}).get("tools") or []

    def call(self, payload: dict) -> dict:
        """``payload`` is ``{"name", "arguments"}`` as render_call returns it. -> parsed result."""
        answer = self._with_session("tools/call", {"name": payload["name"],
                                                   "arguments": payload.get("arguments") or {}})
        return parse_result(answer)


# ---- results -----------------------------------------------------------

def _looks_like_image(item: dict) -> bool:
    return isinstance(item.get("base64Data") or item.get("data"), str) and (
        item.get("type") == "image" or str(item.get("mimeType", "")).startswith("image/"))


def parse_result(answer: dict) -> dict:
    """A JSON-RPC ``tools/call`` answer -> ``{"ok", "text", "images", "data", "error"}``.

    ``raw`` is the untouched ``result`` object. ``text`` is what a script printed
    (the server wraps it as ``{"message": ..., "success": true}``), else the text blocks.
    ``images`` are ``{"mime", "data"}`` (base64) from image content blocks or from
    text blocks that carry ``{"type": "image", "base64Data": ...}`` (the screenshot
    query). ``data`` is the last JSON object found in the text. A call failed when
    the answer is a JSON-RPC error, ``isError`` is set, or the tool's own JSON says
    ``"success": false``.
    """
    out = {"ok": True, "text": "", "images": [], "data": None, "error": "", "raw": None}
    if not isinstance(answer, dict):
        return {**out, "ok": False, "error": "malformed answer"}
    if answer.get("error") is not None:
        failure = answer["error"]
        message = failure.get("message") if isinstance(failure, dict) else str(failure)
        return {**out, "ok": False, "error": str(message or failure)}
    result = answer.get("result")
    if not isinstance(result, dict):
        return {**out, "ok": False, "error": "answer has no result"}
    out["raw"] = result                              # what the project's own parsers read
    texts: list[str] = []
    for item in result.get("content") or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "image" and isinstance(item.get("data"), str):
            out["images"].append({"mime": item.get("mimeType") or "image/png", "data": item["data"]})
            continue
        text = item.get("text")
        if not isinstance(text, str):
            continue
        parsed = _json_object(text)
        if parsed is not None and _looks_like_image(parsed):
            out["images"].append({"mime": parsed.get("mimeType") or "image/png",
                                  "data": parsed.get("base64Data") or parsed.get("data")})
            continue
        if parsed is not None:
            out["data"] = parsed
        texts.append(text)
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        if _looks_like_image(structured):
            out["images"].append({"mime": structured.get("mimeType") or "image/png",
                                  "data": structured.get("base64Data") or structured.get("data")})
        elif out["data"] is None:
            out["data"] = structured
    out["text"] = "\n".join(texts).strip()
    data = out["data"] if isinstance(out["data"], dict) else {}
    if len(texts) == 1 and isinstance(data.get("message"), str) and "success" in data:
        # the server wraps what a script printed: {"message": "<printed>", "success": true}
        out["text"] = data["message"].strip()
    if result.get("isError"):
        out["ok"] = False
        out["error"] = str(data.get("error") or out["text"] or "the tool reported an error")
    elif data.get("success") is False:
        out["ok"] = False
        out["error"] = str(data.get("error") or data.get("message") or out["text"] or "success: false")
    return out


def _json_object(text: str):
    """The text as a JSON object, or its last line that is one; else None."""
    for candidate in [text] + list(reversed(text.splitlines())):
        candidate = candidate.strip()
        if candidate.startswith("{"):
            try:
                value = json.loads(candidate)
            except ValueError:
                continue
            if isinstance(value, dict):
                return value
    return None


def dialog_open(result: dict) -> str | None:
    """Name of the open command dialog from an ``activeCommand`` answer, else None.

    No dialog: ``{"activeCommand": null}`` or ``isDefaultCommand: true`` (reported
    either beside or inside ``activeCommand``).
    """
    data = result.get("data")
    if not isinstance(data, dict):
        return None
    if "activeCommand" not in data:
        # recorded idle shape: {"commandId": "SelectCommand", "commandName": "Select", "isDefaultCommand": true}
        if data.get("isDefaultCommand") is True:
            return None
        if "commandId" in data or "commandName" in data:
            return str(data.get("commandName") or data.get("commandId"))
        return None
    command = data.get("activeCommand")
    if not command:
        return None
    if data.get("isDefaultCommand") is True:
        return None
    if isinstance(command, dict):
        if command.get("isDefaultCommand") is True:
            return None
        return str(command.get("commandName") or command.get("name") or command.get("commandId")
                   or command.get("id") or "a command")
    return str(command)
