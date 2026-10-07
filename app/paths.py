"""Where things live: the repo (or the frozen bundle), the per-user config dir."""
from __future__ import annotations

import os
import sys
from pathlib import Path

from . import APP_NAME

FROZEN = bool(getattr(sys, "frozen", False))
APP_DIR = Path(__file__).resolve().parent            # app
UI_DIR = APP_DIR / "ui"


def install_dir() -> Path:
    """The folder the user sees:  in a checkout, the exe's folder when frozen."""
    if FROZEN:
        return Path(sys.executable).resolve().parent
    return APP_DIR.parent


def resource_root() -> Path:
    """The folder holding projects/<name>/ (repo root, or the bundle's data folder)."""
    override = os.environ.get("FUSION_NEEDLE_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    if FROZEN:
        return Path(getattr(sys, "_MEIPASS", install_dir())).resolve()
    for candidate in APP_DIR.parents:
        if (candidate / "projects").is_dir() and (candidate / "pyproject.toml").is_file():
            return candidate
    return APP_DIR.parent.parent


def project_dir(project: str) -> Path:
    return resource_root() / "projects" / project


def default_log_dir(project: str) -> Path:
    """The session history folder, in the per-user data folder (written only when the history is on)."""
    return config_dir() / "history" / project


def config_dir() -> Path:
    override = os.environ.get("FUSION_NEEDLE_CONFIG_DIR")
    if override:
        return Path(override).expanduser()
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "FusionNeedle"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "FusionNeedle"
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "fusion-needle"


def default_export_dir() -> Path:
    documents = Path.home() / "Documents"
    return (documents if documents.is_dir() else Path.home()) / f"{APP_NAME} exports"

