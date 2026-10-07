"""Bridge to projects/<project>/backend: the renderer, the state reader and the router.

The app never writes Fusion Python; it imports the project's own modules. They
are loaded lazily and every optional piece degrades:

    render.render_call(name, args[, export_dir=...])   required
    render.validate_call(name, args)                   optional
    render.call_info(name) -> {"confirm", "read_only"} optional (wrapper v2)
    render.active_command_call()                       optional
    render.open_document_call(id)                      optional (wrapper v2: second half of open_document)
    mcp_result.dialog_open / search_results            optional (wrapper v2)
    state.state_call() / parse_state / parse_state_dict required
    router.pick_tools(query, state_line) -> [names]    optional (wrapper v2)
"""
from __future__ import annotations

import importlib
import inspect
import sys
from pathlib import Path


class BackendError(Exception):
    pass


class Backend:
    def __init__(self, project_dir: Path):
        self.dir = Path(project_dir) / "backend"
        if not (self.dir / "render.py").is_file():
            raise BackendError(f"no renderer at {self.dir / 'render.py'}")
        path = str(self.dir)
        if path not in sys.path:
            sys.path.insert(0, path)             # the modules import each other by bare name
        try:
            self.render_module = importlib.import_module("render")
            self.state_module = importlib.import_module("state")
        except Exception as failure:
            raise BackendError(f"cannot load the backend in {self.dir}: {type(failure).__name__}: {failure}") \
                from failure
        for module in (self.render_module, self.state_module):
            if Path(getattr(module, "__file__", "")).resolve().parent != self.dir.resolve():
                raise BackendError(f"module {module.__name__!r} was loaded from {module.__file__}, "
                                   f"not from {self.dir}")
        self.result_module = None
        if (self.dir / "mcp_result.py").is_file():
            try:
                self.result_module = importlib.import_module("mcp_result")
            except Exception:
                self.result_module = None
        self.router_module = None
        self.router_error = ""
        if (self.dir / "router.py").is_file():
            try:
                self.router_module = importlib.import_module("router")
            except Exception as failure:         # a broken router must not stop the app
                self.router_error = f"{type(failure).__name__}: {failure}"
        self.RenderError = getattr(self.render_module, "RenderError", ValueError)
        self._accepts_export_dir = self._has_parameter(self.render_module.render_call, "export_dir")

    @staticmethod
    def _has_parameter(function, name: str) -> bool:
        try:
            parameters = inspect.signature(function).parameters
        except (TypeError, ValueError):
            return False
        return name in parameters or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values())

    @staticmethod
    def _make_dir(path: str):
        try:
            Path(path).mkdir(parents=True, exist_ok=True)
        except OSError:
            pass                                 # Fusion reports the unusable folder itself

    # ---- rendering -----------------------------------------------------
    def render(self, name: str, arguments: dict, export_dir: str | None = None) -> dict:
        """``tools/call`` params for Autodesk's server. Raises self.RenderError on a bad call."""
        render_call = self.render_module.render_call
        export_tools = getattr(self.render_module, "EXPORT_TOOLS", None)
        wants_dir = bool(export_dir) and self._accepts_export_dir and (
            name in export_tools if export_tools is not None else False)
        if wants_dir:
            self._make_dir(export_dir)
            payload = render_call(name, arguments, export_dir=export_dir)
        else:
            try:
                payload = render_call(name, arguments)
            except self.RenderError as failure:
                # a renderer without an EXPORT_TOOLS list says so when a tool needs the directory
                if not (export_dir and self._accepts_export_dir and "export_dir" in str(failure)):
                    raise
                self._make_dir(export_dir)
                payload = render_call(name, arguments, export_dir=export_dir)
        if not (isinstance(payload, dict) and isinstance(payload.get("name"), str)
                and isinstance(payload.get("arguments", {}), dict)):
            raise self.RenderError(f"{name}: the renderer returned no tools/call payload")
        return payload

    def validate(self, name: str, arguments: dict) -> str:
        """'' when the call matches the catalogue, else the reason."""
        check = getattr(self.render_module, "validate_call", None)
        if check is None:
            return ""
        try:
            check(name, arguments)
        except Exception as failure:
            return str(failure)
        return ""

    def call_info(self, name: str) -> dict:
        """``confirm``, ``read_only`` and ``undoable`` (None when the project does not say)."""
        info = {"confirm": False, "read_only": False, "undoable": None}
        lookup = getattr(self.render_module, "call_info", None)
        if lookup is not None:
            try:
                found = lookup(name) or {}
                info["confirm"] = bool(found.get("confirm", False))
                info["read_only"] = bool(found.get("read_only", False))
                if "undoable" in found:
                    info["undoable"] = bool(found["undoable"])
            except Exception:
                pass
        return info

    def leaves_undo_step(self, info: dict, payload: dict) -> bool:
        """Does one Fusion undo take this executed call back?

        The project's ``undoable`` flag when it has one; otherwise: a script that
        is not read-only (the model's own undo / redo and direct reads leave none).
        """
        if info.get("undoable") is not None:
            return bool(info["undoable"])
        arguments = payload.get("arguments") or {}
        return (not info.get("read_only") and payload.get("name") == "fusion_mcp_execute"
                and arguments.get("featureType") == "script"
                and not (arguments.get("object") or {}).get("readOnly"))

    def dialog_check(self, result: dict) -> dict:
        """An ``activeCommand`` answer -> ``{"open", "name", "unknown", "reason"}``.

        Uses the project's reader (backend/mcp_result.py) when there is one.
        ``unknown`` is set when the answer cannot be read: the query failed, or
        it has none of the known shapes. The caller treats that as "do not
        send" (README.md) unless the user overrides it
        for the step.
        """
        from .mcp import dialog_open as fallback

        out = {"open": False, "name": None, "unknown": False, "reason": ""}
        if not result.get("ok", True):
            return {**out, "unknown": True, "reason": f"the query failed: {result.get('error') or 'no reason given'}"}
        reader = getattr(self.result_module, "dialog_open", None) if self.result_module else None
        name = fallback(result)
        if reader is None:
            data = result.get("data")
            known = isinstance(data, dict) and ("activeCommand" in data or data.get("isDefaultCommand") is True
                                                or "commandId" in data or "commandName" in data)
            if not known:
                return {**out, "unknown": True, "reason": "unknown activeCommand answer shape"}
            return {**out, "open": name is not None, "name": name}
        try:
            is_open = bool(reader(result.get("raw")))
        except Exception as failure:
            return {**out, "unknown": True, "reason": str(failure) or type(failure).__name__}
        return {**out, "open": is_open, "name": (name or "a command") if is_open else None}

    def dialog_open(self, result: dict) -> str | None:
        """Name of the open dialog, None when Fusion is idle, a text starting ``unknown:`` when unreadable."""
        check = self.dialog_check(result)
        if check["unknown"]:
            return f"unknown: the answer to the dialog check could not be read ({check['reason']})"
        return check["name"] if check["open"] else None

    CAPTURE_KEYS = ("width", "height", "transparentBackground", "antiAliasing")

    @classmethod
    def with_capture_options(cls, payload: dict, options: dict | None) -> dict:
        """Add the app's image options to a rendered screenshot query (capture_view).

        The renderer returns ``fusion_mcp_read {"queryType": "screenshot", "direction": ...}``
        and leaves size and background to the app (backend/README.md, "How each
        tool is sent"). Keys the renderer already set are kept; any other
        payload is returned untouched.
        """
        arguments = payload.get("arguments") or {}
        if not options or payload.get("name") != "fusion_mcp_read" or arguments.get("queryType") != "screenshot":
            return payload
        merged = dict(arguments)
        for key in cls.CAPTURE_KEYS:
            if key in options and key not in merged:
                merged[key] = options[key]
        return {**payload, "arguments": merged}

    DOCUMENT_SWITCHES = ("new_document", "open_document")      # named in README.md

    def switches_document(self, name: str, payload: dict) -> bool:
        """After this call the active document is another one: never roll back across it."""
        arguments = payload.get("arguments") or {}
        operation = (arguments.get("object") or {}).get("operation")
        return name in self.DOCUMENT_SWITCHES or (
            payload.get("name") == "fusion_mcp_execute" and arguments.get("featureType") == "document"
            and operation in ("open", "new"))

    def document_matches(self, name: str, payload: dict, result: dict) -> list[dict] | None:
        """For a rendered document *search* (open_document): the documents found, else None."""
        arguments = payload.get("arguments") or {}
        if not (payload.get("name") == "fusion_mcp_read" and arguments.get("queryType") == "document"
                and arguments.get("operation") == "search"
                and hasattr(self.render_module, "open_document_call")):
            return None
        reader = getattr(self.result_module, "search_results", None) if self.result_module else None
        try:
            if reader is not None:
                found = reader(result.get("raw"))
            else:
                found = [entry for entry in ((result.get("data") or {}).get("results") or [])
                         if isinstance(entry, dict) and isinstance(entry.get("id"), str)]
        except Exception:
            found = []
        return [{"name": str(entry.get("name", entry["id"])), "id": entry["id"]} for entry in found]

    def open_document_call(self, file_id: str) -> dict:
        return self.render_module.open_document_call(file_id)

    def active_command_call(self) -> dict:
        maker = getattr(self.render_module, "active_command_call", None)
        if maker is not None:
            return maker()
        return {"name": "fusion_mcp_read", "arguments": {"queryType": "activeCommand"}}

    @staticmethod
    def undo_call() -> dict:
        return {"name": "fusion_mcp_update", "arguments": {"featureType": "undo"}}

    # ---- state ---------------------------------------------------------
    def state_call(self) -> dict:
        return self.state_module.state_call()

    def parse_state(self, output) -> str:
        return self.state_module.parse_state(output)

    def unhealthy(self, output) -> list:
        reader = getattr(self.state_module, "parse_state_dict", None)
        if reader is None:
            return []
        try:
            return list(reader(output).get("unhealthy") or [])
        except Exception:
            return []

    # ---- tool choice ---------------------------------------------------
    @property
    def has_router(self) -> bool:
        return self.router_module is not None and hasattr(self.router_module, "pick_tools")

    def pick_tools(self, query: str, state_line: str, known: list[str], limit: int) -> list[str] | None:
        """Up to ``limit`` catalogue names for this step, or None to let Needle retrieve."""
        if not self.has_router:
            return None
        try:
            names = self.router_module.pick_tools(query, state_line)
        except Exception as failure:
            self.router_error = f"{type(failure).__name__}: {failure}"
            return None
        if not isinstance(names, (list, tuple)):
            return None
        seen: list[str] = []
        for name in names:
            if isinstance(name, str) and name in known and name not in seen:
                seen.append(name)
        return seen[:limit] or None
