"""The tool project the server answers for: projects/<name>/ with its catalogue."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
DEFAULT_PROJECT = "fusion-mcp"


class ProjectError(Exception):
    """A problem with the installation or a project's files."""


def find_root(start: str | os.PathLike | None = None) -> Path:
    """The folder holding projects/: $STEPSERVER_ROOT, else the first parent that has one."""
    override = os.environ.get("STEPSERVER_ROOT")
    if override:
        root = Path(override).expanduser().resolve()
        if not root.is_dir():
            raise ProjectError(f"STEPSERVER_ROOT={override} is not a directory")
        return root
    here = Path(start or os.getcwd()).resolve()
    packaged = Path(__file__).resolve().parent
    for candidate in (here, *here.parents, *packaged.parents):
        if (candidate / "projects").is_dir():
            return candidate
    raise ProjectError(f"cannot find a projects/ folder above {here}; set STEPSERVER_ROOT")


class Project:
    def __init__(self, name: str = DEFAULT_PROJECT, root: str | os.PathLike | None = None):
        if not NAME_RE.match(name or ""):
            raise ProjectError(f"project name {name!r} is not valid")
        self.name = name
        self.root = Path(root).resolve() if root else find_root()
        self.dir = self.root / "projects" / name
        self.config_path = self.dir / "project.yaml"
        if not self.dir.is_dir():
            raise ProjectError(f"no project {name!r} (missing {self.dir})")
        self.config: dict = {}
        if self.config_path.is_file():
            import yaml

            with open(self.config_path, encoding="utf-8") as handle:
                loaded = yaml.safe_load(handle) or {}
            if not isinstance(loaded, dict):
                raise ProjectError(f"{self.config_path} must be a mapping")
            self.config = loaded
        self._catalogue: list[dict] | None = None

    @property
    def max_tools(self) -> int:
        return int(self.config.get("max_tools_per_step", 5))

    @property
    def catalogue_path(self) -> Path:
        return self.dir / str(self.config.get("tools", "tools/catalogue.json"))

    @property
    def cache_dir(self) -> Path:
        """A per-user folder the engine may write tool embeddings to (the install may be read-only)."""
        base = os.environ.get("STEPSERVER_CACHE") or os.path.join(os.path.expanduser("~"), ".cache", "fusion-needle")
        return Path(base) / "tool-index" / self.name

    def models_dir(self, run_id: str) -> Path:
        if not NAME_RE.match(run_id or ""):
            raise ProjectError(f"model folder name {run_id!r} is not valid")
        return self.dir / "models" / run_id

    @property
    def catalogue(self) -> list[dict]:
        if self._catalogue is None:
            path = self.catalogue_path
            if not path.is_file():
                raise ProjectError(f"tool catalogue not found: {path}")
            with open(path, encoding="utf-8") as handle:
                tools = json.load(handle)
            if not isinstance(tools, list):
                raise ProjectError(f"{path} must be a JSON array of tool schemas")
            tools = [tool.get("function", tool) if isinstance(tool, dict) else tool for tool in tools]
            for tool in tools:
                if not isinstance(tool, dict) or not tool.get("name"):
                    raise ProjectError(f"{path}: every tool needs a name")
            names = [tool["name"] for tool in tools]
            if len(set(names)) != len(names):
                raise ProjectError(f"{path}: duplicate tool names")
            self._catalogue = tools
        return self._catalogue

    @property
    def tools_by_name(self) -> dict[str, dict]:
        return {tool["name"]: tool for tool in self.catalogue}
