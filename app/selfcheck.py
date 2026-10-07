""""Check Fusion": read-only calls against the running Fusion, each reported as pass / warn / fail.

Nothing here modifies the design or moves the camera:

    initialize      the MCP handshake
    tools/list      the three Autodesk tools the backend renders to
    state           the project's read-only state script, parsed into the state line
    activeCommand   the dialog check every modifying step depends on
    screenshot      a small image of the current view (direction "current")
    up axis         which way is up in this document and in the user's preferences

The up-axis script lives here, not in the project backend: the backend assumes
Z up (``top`` = +Z, ``front`` = -Y) and its README lists this as the first
thing to verify on a real Fusion. NOT RUN AGAINST FUSION: the API names in
`ORIENTATION_SCRIPT` are from the Fusion API documentation; each is read on its
own, so one that does not exist shows up under ``errors`` in the raw answer
instead of hiding the others.

A failed item carries the raw answer (base64 removed), so its shape can be sent back.
"""
from __future__ import annotations

import base64
import binascii
import json
import struct
import time

from .diagnostics import raw_text, shape
from .mcp import McpError

EXPECTED_TOOLS = ("fusion_mcp_execute", "fusion_mcp_read", "fusion_mcp_update")
CHECK_IMAGE = (320, 180)

ORIENTATION_SCRIPT = r'''
import json

import adsk.core
import adsk.fusion


def _xyz(vector):
    return [round(vector.x, 6), round(vector.y, 6), round(vector.z, 6)]


def run(_context: str):
    app = adsk.core.Application.get()
    out = {"probe": "orientation", "errors": {}}
    try:
        value = app.preferences.generalPreferences.defaultModelingOrientation
        names = {}
        for name in ("YUpModelingOrientation", "ZUpModelingOrientation"):
            names[getattr(adsk.core.DefaultModelingOrientations, name)] = name
        out["default_modeling_orientation"] = names.get(value, str(value))
    except Exception as failure:
        out["errors"]["default_modeling_orientation"] = repr(failure)
    try:
        viewport = app.activeViewport
        out["front_up"] = _xyz(viewport.frontUpDirection)
        out["front_eye"] = _xyz(viewport.frontEyeDirection)
    except Exception as failure:
        out["errors"]["front_view"] = repr(failure)
    try:
        out["camera_up"] = _xyz(app.activeViewport.camera.upVector)
    except Exception as failure:
        out["errors"]["camera"] = repr(failure)
    try:
        out["document"] = app.activeDocument.name
        design = adsk.fusion.Design.cast(app.activeProduct)
        out["is_design"] = design is not None
    except Exception as failure:
        out["errors"]["document"] = repr(failure)
    print(json.dumps(out))
'''.lstrip()


def orientation_call() -> dict:
    return {"name": "fusion_mcp_execute",
            "arguments": {"featureType": "script", "object": {"script": ORIENTATION_SCRIPT, "readOnly": True}}}


def screenshot_call() -> dict:
    return {"name": "fusion_mcp_read",
            "arguments": {"queryType": "screenshot", "direction": "current", "width": CHECK_IMAGE[0],
                          "height": CHECK_IMAGE[1], "transparentBackground": False}}


def _last_json_line(text: str) -> dict | None:
    for line in reversed((text or "").splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                data = json.loads(line)
            except ValueError:
                continue
            if isinstance(data, dict):
                return data
    return None


def _dominant_axis(vector) -> str | None:
    """'+z', '-y', ... for a unit-ish vector along one axis; None when it is not along an axis."""
    if not (isinstance(vector, list) and len(vector) == 3 and all(isinstance(v, (int, float)) for v in vector)):
        return None
    index = max(range(3), key=lambda i: abs(vector[i]))
    if abs(vector[index]) < 0.9:
        return None
    return ("+" if vector[index] > 0 else "-") + "xyz"[index]


def judge_orientation(data: dict) -> tuple[str, str, dict]:
    """The probe's answer -> (status, text for the user, summary)."""
    preference = {"ZUpModelingOrientation": "z", "YUpModelingOrientation": "y"}.get(
        data.get("default_modeling_orientation"))
    document = _dominant_axis(data.get("front_up"))
    summary = {"document_up": document, "preference_up": preference, "front_up": data.get("front_up"),
               "front_eye": data.get("front_eye"), "camera_up": data.get("camera_up"),
               "document": data.get("document"), "errors": data.get("errors") or {}}
    fix = ("Set Preferences > General > Default modeling orientation to \"Z up\" and work in a new document; "
           "an existing document keeps the orientation it was created with.")
    if document is not None and document != "+z":
        return "fail", (f"This document's front view has {document} up, but the backend assumes Z up: face names "
                        f"(top, front, ...), the top / bottom edge sets and the view directions would not match "
                        f"what you see. Nothing should be built here. {fix}"), summary
    if document == "+z":
        if preference == "y":
            return "warn", ("This document is Z up, as the backend assumes, but your preference is \"Y up\": "
                            f"new documents will not match. {fix}"), summary
        note = "" if preference == "z" else " (the default-orientation preference could not be read)"
        return "pass", f"Z up: this document matches what the backend assumes{note}.", summary
    if preference == "y":
        return "fail", (f"The document's orientation could not be read and your preference is \"Y up\"; the backend "
                        f"assumes Z up. {fix}"), summary
    if preference == "z":
        return "warn", ("Your preference is \"Z up\", but this document's own orientation could not be read; "
                        "look at the view cube: TOP must face +Z."), summary
    return "fail", ("Neither the document's orientation nor the preference could be read. Look at the view cube: "
                    "TOP must face +Z, the backend assumes Z up."), summary


def png_size(data_base64: str) -> tuple[int, int, int] | None:
    """(width, height, bytes) of a base64 PNG; None when it is not one."""
    try:
        raw = base64.b64decode(data_base64, validate=False)
    except (binascii.Error, ValueError):
        return None
    if len(raw) < 24 or raw[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    width, height = struct.unpack(">II", raw[16:24])
    return width, height, len(raw)


class Check:
    def __init__(self, fusion, backend, keep_images=None, progress=None):
        self.fusion = fusion
        self.backend = backend
        self.keep_images = keep_images
        self.progress = progress
        self.items: list[dict] = []
        self.summary: dict = {}

    def _add(self, key: str, label: str, status: str, detail: str, started: float | None = None, raw=None,
             **extra) -> dict:
        item = {"id": key, "label": label, "status": status, "detail": detail,
                "ms": None if started is None else round((time.perf_counter() - started) * 1000, 1), **extra}
        if raw is not None and status != "pass":
            item["raw"] = raw_text(raw)
            item["shape"] = shape(raw)
        self.items.append(item)
        if self.progress is not None:
            self.progress(label)
        return item

    def _call(self, key: str, label: str, payload: dict):
        """Send one tools/call. -> (parsed result or None, started)."""
        started = time.perf_counter()
        try:
            return self.fusion.call(payload), started
        except McpError as failure:
            self._add(key, label, "fail", f"no usable answer: {failure}", started)
            return None, started

    def run(self) -> dict:
        began = time.time()
        if self._initialize():
            self._tools()
            self._state()
            self._active_command()
            self._screenshot()
            self._orientation()
        else:
            for key, label in (("tools", "tools/list"), ("state", "Read-only state script"),
                               ("active_command", "Dialog check (activeCommand)"), ("screenshot", "Screenshot"),
                               ("up_axis", "Up axis")):
                self._add(key, label, "skip", "not run: no connection")
        statuses = [item["status"] for item in self.items]
        overall = "fail" if "fail" in statuses else "warn" if "warn" in statuses or "skip" in statuses else "pass"
        return {"overall": overall, "items": self.items, "summary": self.summary,
                "started": began, "seconds": round(time.time() - began, 2), "url": self.fusion.url}

    def _initialize(self) -> bool:
        started = time.perf_counter()
        try:
            result = self.fusion.connect()
        except McpError as failure:
            self._add("initialize", "MCP handshake (initialize)", "fail",
                      f"{failure}. Is Fusion running, with its MCP server enabled, at {self.fusion.url}?", started)
            return False
        info = result.get("serverInfo") if isinstance(result, dict) else None
        if not self.fusion.session_id:
            self._add("initialize", "MCP handshake (initialize)", "fail",
                      "answered, but without an MCP-Session-Id header; later requests would be refused", started,
                      raw=result)
            return False
        if not isinstance(info, dict):
            self._add("initialize", "MCP handshake (initialize)", "warn", "answered, but without serverInfo",
                      started, raw=result)
            return True
        self.summary["server"] = info
        self.summary["protocol"] = result.get("protocolVersion")
        self._add("initialize", "MCP handshake (initialize)", "pass",
                  f"{info.get('name', '?')} {info.get('version', '')}, protocol {result.get('protocolVersion', '?')}",
                  started)
        return True

    def _tools(self):
        started = time.perf_counter()
        try:
            answer = self.fusion.request("tools/list", {})
        except McpError as failure:
            self._add("tools", "tools/list", "fail", str(failure), started)
            return
        tools = (answer.get("result") or {}).get("tools") if isinstance(answer.get("result"), dict) else None
        names = [tool.get("name") for tool in tools if isinstance(tool, dict)] if isinstance(tools, list) else None
        if names is None:
            self._add("tools", "tools/list", "fail", "the answer holds no tools list", started, raw=answer)
            return
        self.summary["tools"] = names
        missing = [name for name in EXPECTED_TOOLS if name not in names]
        if missing:
            self._add("tools", "tools/list", "fail",
                      f"missing {', '.join(missing)}; the server offers {', '.join(map(str, names)) or 'nothing'}",
                      started, raw=answer)
        else:
            self._add("tools", "tools/list", "pass", ", ".join(map(str, names)), started)

    def _state(self):
        label = "Read-only state script"
        result, started = self._call("state", label, self.backend.state_call())
        if result is None:
            return
        if not result["ok"]:
            self._add("state", label, "fail", f"the script failed: {result['error']}", started,
                      raw=result.get("raw") or {"error": result["error"]})
            return
        try:
            line = self.backend.parse_state(result["raw"])
        except Exception as failure:
            self._add("state", label, "fail", f"the answer could not be parsed: {failure}", started,
                      raw=result.get("raw"))
            return
        self.summary["state"] = line
        self._add("state", label, "pass", line, started)

    def _active_command(self):
        label = "Dialog check (activeCommand)"
        result, started = self._call("active_command", label, self.backend.active_command_call())
        if result is None:
            return
        check = self.backend.dialog_check(result)
        raw = result.get("raw") or {"error": result.get("error")}
        self.summary["active_command"] = result.get("data")
        if check["unknown"]:
            self._add("active_command", label, "fail",
                      f"the answer could not be read ({check['reason']}). Every modifying step would stop at "
                      "\"dialog\" until this shape is known: send the diagnostics back.", started, raw=raw)
        elif check["open"]:
            self._add("active_command", label, "warn",
                      f"readable; a command dialog is open right now: {check['name']}. Close it and check again "
                      "to see the idle answer.", started, raw=raw)
        else:
            self._add("active_command", label, "pass", "readable; no command dialog is open", started)

    def _screenshot(self):
        label = "Screenshot"
        result, started = self._call("screenshot", label, screenshot_call())
        if result is None:
            return
        if not result["ok"] or not result["images"]:
            why = result["error"] if not result["ok"] else "the answer holds no image in a known shape"
            self._add("screenshot", label, "fail", why, started, raw=result.get("raw") or {"error": result["error"]})
            return
        image = result["images"][0]
        size = png_size(image["data"])
        content = (result.get("raw") or {}).get("content") or []
        how = "image block" if any(isinstance(b, dict) and b.get("type") == "image" for b in content) else "JSON text"
        kept = self.keep_images(result["images"][:1]) if self.keep_images is not None else []
        if size is None:
            self._add("screenshot", label, "warn", f"an image came back as {how}, but it is not a readable PNG "
                      f"({image.get('mime')})", started, raw=result.get("raw"), images=kept)
            return
        self.summary["screenshot"] = {"asked": list(CHECK_IMAGE), "got": list(size[:2]), "bytes": size[2], "as": how}
        honoured = tuple(size[:2]) == CHECK_IMAGE
        self._add("screenshot", label, "pass" if honoured else "warn",
                  f"PNG {size[0]}x{size[1]}, {size[2]} bytes, sent as {how}"
                  + ("" if honoured else f"; {CHECK_IMAGE[0]}x{CHECK_IMAGE[1]} was asked for, so width / height "
                                         "are not applied as sent"), started, images=kept)

    def _orientation(self):
        label = "Up axis"
        result, started = self._call("up_axis", label, orientation_call())
        if result is None:
            return
        if not result["ok"]:
            self._add("up_axis", label, "fail", f"the read-only script failed: {result['error']}. Look at the view "
                      "cube: TOP must face +Z, the backend assumes Z up.", started,
                      raw=result.get("raw") or {"error": result["error"]})
            return
        data = _last_json_line(result.get("text") or "")
        if data is None or data.get("probe") != "orientation":
            self._add("up_axis", label, "fail", "the script's answer could not be read", started,
                      raw=result.get("raw"))
            return
        status, detail, summary = judge_orientation(data)
        self.summary["orientation"] = summary
        self._add("up_axis", label, status, detail, started, raw=data, values=summary)


def run_check(fusion, backend, keep_images=None, progress=None) -> dict:
    return Check(fusion, backend, keep_images, progress).run()
