"""Render a narrow tool call into the MCP ``tools/call`` payload of Autodesk's
Fusion MCP server (http://127.0.0.1:27182/mcp).

    render_call("draw_rectangle", {"width": 40, "height": 30})
    -> {"name": "fusion_mcp_execute",
        "arguments": {"featureType": "script", "object": {"script": "..."}}}

    render_call("undo", {})
    -> {"name": "fusion_mcp_update", "arguments": {"featureType": "undo"}}

    render_call("capture_view", {"direction": "top"})
    -> {"name": "fusion_mcp_read", "arguments": {"queryType": "screenshot", "direction": "top"}}

    render_call("export_model", {"format": "step"}, export_dir="C:/Users/me/exports")

    call_info("save_document")
    -> {"confirm": True, "read_only": False, "undoable": False, "group": "document"}

Safety model: the model's output never reaches the script as code.

* The call is validated against tools/catalogue.json first (known tool, known
  parameters only, required present, exact types, enum membership, bounds,
  finite numbers, string pattern and length).
* The script is ``ARGS = {...}`` + ``GIVEN = (...)`` + templates/_common.py +
  templates/<tool>.py. ARGS holds ints and floats (written with repr of exact
  int / float objects), enum strings taken from the catalogue's own enum
  lists, and name strings: these must match the catalogue pattern in full and
  a fixed safe alphabet (letters, digits, space, ``_ . -``; first character a
  letter, digit or underscore), and are written
  as a quoted literal. Keys come from the catalogue. No template is
  string-formatted.
* ARGS has the schema defaults applied; GIVEN lists the parameters the call
  actually carried, for the few places where "not stated" matters.
* The export directory is not an argument of any tool. The app passes it to
  ``render_call(..., export_dir=...)``; it becomes one more quoted literal.

Lengths stay in mm here; the templates convert to Fusion's centimetres (MM).
Standard library only, except ``call_info`` (reads tools/toolsets.yaml, pyyaml).
"""
from __future__ import annotations

import json
import math
import ntpath
import posixpath
import re
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent
TEMPLATE_DIR = BACKEND_DIR / "templates"
CATALOGUE_PATH = BACKEND_DIR.parent / "tools" / "catalogue.json"
TOOLSETS_PATH = BACKEND_DIR.parent / "tools" / "toolsets.yaml"

# The server answers with the protocol version the client asked for: 2025-11-25 in
# a recorded MCP handshake, 2024-11-05 in third-party reference material for Fusion's MCP server.
PROTOCOL_VERSION = "2025-11-25"

UPDATE_TOOLS = {"undo": "undo", "redo": "redo"}     # tool -> fusion_mcp_update featureType
# Scripts sent with readOnly: true (enforced by Fusion: a modification raises).
READ_ONLY_SCRIPTS = frozenset({"list_parameters", "measure_body", "check_timeline", "fit_view"})
# Tools that map to an MCP call without a script.
DIRECT_TOOLS = frozenset({"capture_view", "save_document", "open_document"})
EXPORT_TOOLS = frozenset({"export_model"})          # need export_dir from the app
MAX_ABS = 1.0e6                                     # mm / degrees / count sanity bound
MAX_STRING = 64
# A leading underscore is allowed because set_parameter's catalogue pattern allows it (_gap);
# the file and document name patterns of the catalogue still demand a letter or digit first.
SAFE_STRING = re.compile(r"[A-Za-z0-9_][A-Za-z0-9 _.\-]*")
RESERVED_FILE_NAMES = frozenset(["con", "prn", "aux", "nul"] + [f"com{i}" for i in range(1, 10)]
                                + [f"lpt{i}" for i in range(1, 10)])
FILE_NAME_PARAMS = {("export_model", "name")}

_catalogue_cache: dict | None = None
_toolsets_cache: dict | None = None


class RenderError(ValueError):
    """The call does not match the catalogue; nothing was rendered."""


def catalogue() -> dict:
    global _catalogue_cache
    if _catalogue_cache is None:
        with open(CATALOGUE_PATH, encoding="utf-8") as handle:
            _catalogue_cache = {tool["name"]: tool for tool in json.load(handle)}
    return _catalogue_cache


def toolsets() -> dict:
    """tools/toolsets.yaml, checked against the catalogue."""
    global _toolsets_cache
    if _toolsets_cache is None:
        import yaml

        with open(TOOLSETS_PATH, encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
        names = set(catalogue())
        for key in ("confirm", "read_only", "no_undo"):
            data.setdefault(key, [])
            unknown = set(data[key]) - names
            if unknown:
                raise RenderError(f"toolsets.yaml {key}: unknown tools {sorted(unknown)}")
        grouped: dict = {}
        for group, members in (data.get("groups") or {}).items():
            for member in members:
                if member not in names:
                    raise RenderError(f"toolsets.yaml groups.{group}: unknown tool {member!r}")
                if member in grouped:
                    raise RenderError(f"toolsets.yaml: {member!r} is in two groups")
                grouped[member] = group
        missing = names - set(grouped)
        if missing:
            raise RenderError(f"toolsets.yaml: no group for {sorted(missing)}")
        if set(data["read_only"]) & set(data["no_undo"]):
            raise RenderError("toolsets.yaml: a tool is both read_only and no_undo")
        data["_group_of"] = grouped
        _toolsets_cache = data
    return _toolsets_cache


def call_info(name) -> dict:
    """How the app treats a call of this tool.

    confirm   - ask the user before executing
    read_only - changes nothing: no dialog check, not counted in a rollback
    undoable  - leaves one undo step, so a rollback sends one undo for it
    group     - the tool's group in toolsets.yaml
    """
    if type(name) is not str or name not in catalogue():
        raise RenderError(f"unknown tool {name!r}")
    sets = toolsets()
    read_only = name in sets["read_only"]
    return {"confirm": name in sets["confirm"], "read_only": read_only,
            "undoable": not read_only and name not in sets["no_undo"] and name not in UPDATE_TOOLS,
            "group": sets["_group_of"][name]}


def _check_string(name, key, spec, value) -> str:
    """A name string: exact str, the catalogue pattern in full, and the fixed safe alphabet."""
    if type(value) is not str:
        raise RenderError(f"{name}.{key}: expected a string, got {type(value).__name__}")
    pattern, limit = spec.get("pattern"), spec.get("maxLength")
    if type(pattern) is not str or type(limit) is not int:
        raise RenderError(f"{name}.{key}: a string parameter needs a pattern and a maxLength to be renderable")
    if not value or len(value) > min(limit, MAX_STRING):
        raise RenderError(f"{name}.{key}: must be 1 to {min(limit, MAX_STRING)} characters")
    # fullmatch: "$" alone would let a trailing newline through
    if re.fullmatch(pattern, value) is None or SAFE_STRING.fullmatch(value) is None:
        raise RenderError(f"{name}.{key}: {value!r} does not match the allowed name pattern")
    if value != value.strip() or "  " in value or ".." in value:
        raise RenderError(f"{name}.{key}: {value!r} is not a clean name")
    if (name, key) in FILE_NAME_PARAMS and value.lower() in RESERVED_FILE_NAMES:
        raise RenderError(f"{name}.{key}: {value!r} is a reserved file name")
    return value


def validate_call(name, arguments) -> dict:
    """Return the validated arguments (only those given), or raise RenderError."""
    tools = catalogue()
    if type(name) is not str or name not in tools:
        raise RenderError(f"unknown tool {name!r}")
    if arguments is None:
        arguments = {}
    if type(arguments) is not dict:
        raise RenderError("arguments must be an object")
    schema = tools[name]["parameters"]
    props = schema.get("properties", {})
    for key in arguments:
        if type(key) is not str or key not in props:
            raise RenderError(f"{name}: unknown parameter {key!r}")
    for key in schema.get("required", []):
        if key not in arguments:
            raise RenderError(f"{name}: missing required parameter {key!r}")
    clean = {}
    for key, spec in props.items():
        if key not in arguments:
            continue
        value = arguments[key]
        kind = spec.get("type")
        if "enum" in spec:
            if type(value) is not str or value not in spec["enum"]:
                raise RenderError(f"{name}.{key}: {value!r} is not one of {spec['enum']}")
            clean[key] = spec["enum"][spec["enum"].index(value)]    # the catalogue's own string
            continue
        if kind == "string":
            clean[key] = _check_string(name, key, spec, value)
            continue
        if kind == "integer":
            if type(value) is not int:
                raise RenderError(f"{name}.{key}: expected an integer, got {type(value).__name__}")
        elif kind == "number":
            if type(value) not in (int, float):
                raise RenderError(f"{name}.{key}: expected a number, got {type(value).__name__}")
            if not math.isfinite(value):
                raise RenderError(f"{name}.{key}: not a finite number")
        else:
            raise RenderError(f"{name}.{key}: parameter type {kind!r} is not renderable")
        if abs(value) > MAX_ABS:
            raise RenderError(f"{name}.{key}: {value!r} is out of range")
        if "minimum" in spec and value < spec["minimum"]:
            raise RenderError(f"{name}.{key}: {value!r} is below the minimum {spec['minimum']}")
        if "exclusiveMinimum" in spec and value <= spec["exclusiveMinimum"]:
            raise RenderError(f"{name}.{key}: {value!r} must be greater than {spec['exclusiveMinimum']}")
        if "maximum" in spec and value > spec["maximum"]:
            raise RenderError(f"{name}.{key}: {value!r} is above the maximum {spec['maximum']}")
        if "exclusiveMaximum" in spec and value >= spec["exclusiveMaximum"]:
            raise RenderError(f"{name}.{key}: {value!r} must be less than {spec['exclusiveMaximum']}")
        clean[key] = value
    return clean


def _literal(value, spec: dict) -> str:
    """Python source for a validated value. Only exact int / float / str reach here."""
    if type(value) is int:
        return repr(value)
    if type(value) is float:
        return repr(value)
    if type(value) is str:
        if "enum" in spec:
            if not value.replace("_", "").isalnum() or not value.isascii():
                raise RenderError(f"enum value {value!r} is not a plain identifier")
            return '"' + value + '"'
        if SAFE_STRING.fullmatch(value) is None or len(value) > MAX_STRING:
            raise RenderError(f"string {value!r} is outside the safe alphabet")
        return '"' + value + '"'          # no quote, backslash or control character can be in it
    raise RenderError(f"cannot render a {type(value).__name__}")


def _default_ok(value) -> bool:
    return type(value) in (int, float, str)


def _export_dir_literal(export_dir) -> str:
    """The app's export directory as a Python literal. Not model output, still checked."""
    if type(export_dir) is not str or not export_dir:
        raise RenderError("export_model needs export_dir, an absolute directory chosen by the app")
    if len(export_dir) > 240 or any(ord(ch) < 32 or ord(ch) == 127 for ch in export_dir):
        raise RenderError("export_dir is too long or holds control characters")
    if not (posixpath.isabs(export_dir) or ntpath.isabs(export_dir)):
        raise RenderError("export_dir must be an absolute path")
    if ".." in re.split(r"[\\/]", export_dir):
        raise RenderError("export_dir must not contain '..'")
    return repr(export_dir)


def render_script(name, arguments, export_dir=None) -> str:
    """The Python script for one tool call that runs as a script."""
    given = validate_call(name, arguments)
    if name in UPDATE_TOOLS:
        raise RenderError(f"{name} has no script; it maps to fusion_mcp_update")
    if name in DIRECT_TOOLS:
        raise RenderError(f"{name} has no script; it maps to an MCP call directly")
    if export_dir is not None and name not in EXPORT_TOOLS:
        raise RenderError(f"{name} takes no export_dir")
    props = catalogue()[name]["parameters"].get("properties", {})
    full = {}
    for key, spec in props.items():
        if key in given:
            full[key] = given[key]
        elif "default" in spec and _default_ok(spec["default"]):
            full[key] = spec["default"]
    args_src = ", ".join(f'"{key}": {_literal(value, props[key])}' for key, value in full.items())
    given_src = "".join(f'"{key}", ' for key in full if key in given)
    header = (f"# {name}: rendered by backend/render.py - do not edit by hand\n"
              f"ARGS = {{{args_src}}}\n"
              f"GIVEN = ({given_src})\n")
    if name in EXPORT_TOOLS:
        header += f"EXPORT_DIR = {_export_dir_literal(export_dir)}\n"
    common = (TEMPLATE_DIR / "_common.py").read_text(encoding="utf-8")
    template = (TEMPLATE_DIR / f"{name}.py").read_text(encoding="utf-8")
    return header + "\n" + common + "\n\n" + template


def render_call(name, arguments, export_dir=None) -> dict:
    """The exact ``tools/call`` params for Autodesk's server.

    ``export_dir`` is required for export_model and refused for every other tool.
    ``open_document`` renders the document *search*; pick a result with
    ``mcp_result.search_results`` and open it with ``open_document_call(id)``.
    """
    if type(name) is str and name in UPDATE_TOOLS:
        validate_call(name, arguments)
        if export_dir is not None:
            raise RenderError(f"{name} takes no export_dir")
        return {"name": "fusion_mcp_update", "arguments": {"featureType": UPDATE_TOOLS[name]}}
    if type(name) is str and name in DIRECT_TOOLS:
        given = validate_call(name, arguments)
        if export_dir is not None:
            raise RenderError(f"{name} takes no export_dir")
        if name == "capture_view":
            direction = given.get("direction", catalogue()[name]["parameters"]["properties"]["direction"]["default"])
            return {"name": "fusion_mcp_read", "arguments": {"queryType": "screenshot", "direction": direction}}
        if name == "save_document":
            return {"name": "fusion_mcp_execute",
                    "arguments": {"featureType": "document", "object": {"operation": "save"}}}
        return {"name": "fusion_mcp_read",
                "arguments": {"queryType": "document", "operation": "search", "name": given["name"]}}
    script = render_script(name, arguments, export_dir)
    body = {"script": script}
    if name in READ_ONLY_SCRIPTS:
        body["readOnly"] = True
    return {"name": "fusion_mcp_execute", "arguments": {"featureType": "script", "object": body}}


def open_document_call(file_id) -> dict:
    """Second half of open_document: open one result of the search by its id.

    The id comes from Fusion (``mcp_result.search_results``), never from the model.
    """
    if type(file_id) is not str or not 0 < len(file_id) <= 512 \
            or re.fullmatch(r"[\x21-\x7e]+", file_id) is None:
        raise RenderError("file id must be printable ASCII without spaces")
    return {"name": "fusion_mcp_execute",
            "arguments": {"featureType": "document", "object": {"operation": "open", "fileId": file_id}}}


def active_command_call() -> dict:
    """Check this before a modifying script: scripts fail while a command dialog is open.

    Read the answer with ``mcp_result.dialog_open``.
    """
    return {"name": "fusion_mcp_read", "arguments": {"queryType": "activeCommand"}}


def open_documents_call() -> dict:
    """Lists the open documents (``isActive``, ``isModified``, ``isSaved`` per entry)."""
    return {"name": "fusion_mcp_read", "arguments": {"queryType": "document", "operation": "open"}}


def jsonrpc(params: dict, request_id: int = 1) -> dict:
    """Wrap ``render_call`` output into a JSON-RPC ``tools/call`` request."""
    return {"jsonrpc": "2.0", "id": request_id, "method": "tools/call", "params": params}


def initialize_request(request_id: int = 0, client: str = "needle-fusion-app", version: str = "0.1") -> dict:
    """First request of a session. The answer carries the ``MCP-Session-Id`` response header,
    which every later request must send back (the server answers HTTP 400 without it)."""
    return {"jsonrpc": "2.0", "id": request_id, "method": "initialize",
            "params": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                       "clientInfo": {"name": client, "version": version}}}


def initialized_notification() -> dict:
    """Second message of a session (no id; the server answers HTTP 202)."""
    return {"jsonrpc": "2.0", "method": "notifications/initialized"}


if __name__ == "__main__":
    import sys

    tool = sys.argv[1]
    args = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
    payload = render_call(tool, args, export_dir=sys.argv[3] if len(sys.argv) > 3 else None)
    if payload["name"] == "fusion_mcp_execute" and "script" in payload["arguments"]["object"]:
        print(payload["arguments"]["object"]["script"])
    else:
        print(json.dumps(payload))
