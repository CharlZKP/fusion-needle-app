"""Evidence for the owner to send back: what was sent to Fusion and what it answered.

Nothing in this project has been run against a real Fusion yet, so the first
real session has to leave a record that can be read afterwards:

* the last N MCP requests and answers, raw (scripts included, base64 images
  replaced by their size), kept in memory;
* notable events (an ``activeCommand`` answer that could not be read, the result
  of "Check Fusion"), appended to ``<config dir>/diagnostics/events.jsonl``;
* ``export()``: one zip with both, the app / engine versions and the settings
  without the server token.

Standard library only.
"""
from __future__ import annotations

import collections
import datetime
import json
import platform
import sys
import threading
import zipfile
from pathlib import Path

IMAGE_KEYS = ("data", "base64Data", "blob")
MAX_TEXT = 200_000                  # one string in a record (a script is a few thousand characters)
MAX_EVENTS_FILE = 2_000_000         # bytes; the file is rotated once to events.1.jsonl
BASE64 = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=-_\r\n")


def _now() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="milliseconds")


def _looks_base64(text: str) -> bool:
    return len(text) > 64 and all(char in BASE64 for char in text[:400])


def scrub(value):
    """A copy with base64 payloads replaced by their size. Everything else stays as it was sent."""
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if isinstance(item, str) and key in IMAGE_KEYS and _looks_base64(item):
                out[key] = f"<base64 removed: {len(item)} characters, about {len(item) * 3 // 4} bytes>"
            else:
                out[key] = scrub(item)
        return out
    if isinstance(value, (list, tuple)):
        return [scrub(item) for item in value]
    if isinstance(value, str):
        stripped = value.lstrip()
        if len(value) > 200 and stripped.startswith("{"):       # JSON inside a text block (screenshot, per the docs)
            try:
                inner = json.loads(value)
            except ValueError:
                inner = None
            if isinstance(inner, dict):
                cleaned = scrub(inner)
                if cleaned != inner:
                    return json.dumps(cleaned)
        if len(value) > MAX_TEXT:
            return value[:MAX_TEXT] + f"<cut: {len(value)} characters in all>"
    return value


def raw_text(value, limit: int = 6000) -> str:
    """Scrubbed JSON of an answer, for showing to the user."""
    try:
        text = json.dumps(scrub(value), ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        text = repr(value)
    return text if len(text) <= limit else text[:limit] + f"\n<cut: {len(text)} characters in all>"


def shape(value, depth: int = 0):
    """Keys and types of an answer without its values, to describe a shape in one line."""
    if isinstance(value, dict):
        if depth >= 4:
            return "{...}"
        return {str(key): shape(item, depth + 1) for key, item in value.items()}
    if isinstance(value, list):
        return [shape(value[0], depth + 1)] if value else []
    return type(value).__name__


def redact(text: str, secrets) -> str:
    for secret in secrets:
        if isinstance(secret, str) and len(secret) >= 4:
            text = text.replace(secret, "<removed>")
    return text


class Diagnostics:
    def __init__(self, directory: Path, keep: int = 200):
        self.directory = Path(directory)
        self.calls: collections.deque = collections.deque(maxlen=max(10, int(keep)))
        self.events: collections.deque = collections.deque(maxlen=200)
        self.total_calls = 0
        self._lock = threading.Lock()

    @property
    def events_path(self) -> Path:
        return self.directory / "events.jsonl"

    # ---- recording -----------------------------------------------------
    def record_call(self, entry: dict):
        """Called by mcp.FusionClient for every request. Repeats of the same failure are counted, not stored."""
        entry = {"time": _now(), **scrub(entry)}
        with self._lock:
            self.total_calls += 1
            last = self.calls[-1] if self.calls else None
            if (last is not None and entry.get("error") and last.get("error") == entry["error"]
                    and last.get("method") == entry.get("method")):
                last["repeats"] = last.get("repeats", 0) + 1
                last["last_time"] = entry["time"]
                return
            self.calls.append(entry)

    def event(self, kind: str, **data) -> dict:
        entry = {"time": _now(), "kind": kind, **scrub(data)}
        with self._lock:
            self.events.append(entry)
            try:
                self.directory.mkdir(parents=True, exist_ok=True)
                path = self.events_path
                if path.is_file() and path.stat().st_size > MAX_EVENTS_FILE:
                    path.replace(path.with_name("events.1.jsonl"))
                with open(path, "a", encoding="utf-8") as handle:
                    handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
            except OSError:
                pass                                    # the copy in memory still goes into an export
        return entry

    # ---- reading -------------------------------------------------------
    def summary(self, count: int = 40) -> dict:
        with self._lock:
            calls = list(self.calls)[-count:]
            events = list(self.events)[-20:]
        rows = []
        for call in calls:
            request = call.get("request") or {}
            arguments = request.get("arguments") or {}
            what = request.get("name") or ""
            detail = arguments.get("queryType") or arguments.get("featureType") or ""
            rows.append({"time": call["time"], "method": call.get("method", ""), "tool": what, "kind": detail,
                         "status": call.get("http_status"), "ms": call.get("ms"), "error": call.get("error", ""),
                         "repeats": call.get("repeats", 0)})
        return {"calls": rows, "kept": len(self.calls), "capacity": self.calls.maxlen, "total": self.total_calls,
                "events": events, "folder": str(self.directory), "events_file": str(self.events_path)}

    # ---- export --------------------------------------------------------
    def export(self, info: dict, extra_files: dict | None = None, secrets=()) -> Path:
        """Write ``fusion-needle-diagnostics-<time>.zip`` into the diagnostics folder. -> its path.

        ``info`` goes into ``info.json``; ``extra_files`` maps a name in the zip
        to text. Every text is passed through `redact` with ``secrets``.
        """
        self.directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        path = self.directory / f"fusion-needle-diagnostics-{stamp}.zip"
        number = 1
        while path.exists():
            number += 1
            path = self.directory / f"fusion-needle-diagnostics-{stamp}-{number}.zip"
        with self._lock:
            calls = list(self.calls)
            events = list(self.events)
        about = {"written": _now(), "python": sys.version, "platform": platform.platform(),
                 "machine": platform.machine(), "calls_kept": len(calls), "calls_capacity": self.calls.maxlen,
                 "calls_total": self.total_calls, **info}
        files = {
            "README.txt": README,
            "info.json": json.dumps(about, ensure_ascii=False, indent=2, default=str),
            "mcp-calls.jsonl": "".join(json.dumps(call, ensure_ascii=False, default=str) + "\n" for call in calls),
            "events-this-launch.jsonl": "".join(json.dumps(event, ensure_ascii=False, default=str) + "\n"
                                                for event in events),
        }
        for name in ("events.jsonl", "events.1.jsonl"):
            try:
                files[name] = (self.directory / name).read_text(encoding="utf-8", errors="replace")
            except OSError:
                pass
        files.update(extra_files or {})
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, text in files.items():
                archive.writestr(name, redact(str(text), secrets))
        return path


README = """Fusion Needle diagnostics

info.json                 app, Python, OS, model server (engine version), Fusion server, settings.
                          The server token is not in it (only whether one is set).
mcp-calls.jsonl           the last requests sent to Fusion's MCP server and the raw answers, oldest first.
                          Scripts are included in full. Base64 image data is replaced by its size.
events-this-launch.jsonl  notable events of this launch (unreadable dialog check, Check Fusion results,
                          and every answer the call guard did not let through: kind "guard").
events.jsonl              the same, across launches.
fusion-check.json         the last "Check Fusion" result, when one was run.
session.json              the steps of the current goal as the page shows them, each with the guard's verdict.
model-server.log          tail of the model server's output.

It does contain: the feature texts you typed, document and file names, folder paths on this computer.
"""
