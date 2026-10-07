"""Settings, kept as JSON in the per-user config dir (never in the repo)."""
from __future__ import annotations

import copy
import json
import os
import threading
from pathlib import Path

from . import paths

DEFAULTS: dict = {
    "project": "fusion-mcp",
    "fusion_url": "http://127.0.0.1:27182/mcp",
    "model": {
        "mode": "auto",                 # auto | spawn (start the step server here; auto means the same) | url
        "weights": "auto",              # spawn: 'auto' (the published model, downloaded once), 'base', or a .cact path
        "url": "",                      # url: an already-running the step server
        "token": "",                    # url: its STEPSERVER_TOKEN, if any
        # Builds that download the model (app/modelstore.py; not this workspace): which model 'auto' means.
        # Changed only through the "Model source" window, which asks the user to confirm (server.py).
        "source": "official",           # official | custom
        "custom": {"repo": "", "revision": "", "file": "", "bytes": 0, "sha256": ""},
        "hf_token": "",                 # access token for a private custom repository, if any
        "python": "",                   # interpreter for the child (default: this one)
        "agents": 4,
        "warm": False,                  # warm-up step at start (slow with a large catalogue)
    },
    "toolset": "auto",                  # auto | router | catalogue (omit `tools`, Needle retrieves 5)
    "confirm_over_calls": 3,            # ask before running a step with more calls than this
    "save_history": False,             # session history: one JSON line per accepted step (off by default)
    "export_dir": "",                   # where export tools write (default: Documents/Fusion Needle exports)
    "log_dir": "",                      # session history folder (default: <per-user data folder>/history)
    "window": "auto",                   # auto | native | browser | none
    "capture_width": 1280,              # capture_view image size in pixels (Fusion accepts 32 to 4096)
    "capture_height": 720,
    "capture_transparent": False,       # False: opaque background, readable when shown inline
    "diagnostics_keep": 200,            # how many MCP requests / answers the Diagnostics export holds
}

SECRET_KEYS = (("model", "token"), ("model", "hf_token"))
MODEL_MODES = ("auto", "spawn", "url")
MODEL_SOURCES = ("official", "custom")


def _coerce(default, value):
    """`value` if it has the type of `default`, else `default`."""
    if isinstance(default, bool):
        return value if isinstance(value, bool) else default
    if isinstance(default, int):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return default
        return int(value)
    return value if isinstance(value, type(default)) else default


def _merge(base: dict, extra: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (extra or {}).items():
        if key not in out:
            continue                                    # unknown keys are dropped
        if isinstance(out[key], dict):
            if isinstance(value, dict):
                out[key] = _merge(out[key], value)
        else:
            out[key] = _coerce(out[key], value)
    return out


def _normalise(data: dict) -> dict:
    """Values an older version could store and this one no longer has."""
    if data["model"]["mode"] not in MODEL_MODES:        # "bundle" (protected models) is gone
        data["model"]["mode"] = "auto"
    if data["model"]["source"] not in MODEL_SOURCES:
        data["model"]["source"] = "official"
    return data


class Settings:
    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else paths.config_dir() / "settings.json"
        self._lock = threading.Lock()
        self.data = copy.deepcopy(DEFAULTS)
        self.load_error = ""
        self.load()

    def load(self):
        try:
            with open(self.path, encoding="utf-8") as handle:
                stored = json.load(handle)
        except FileNotFoundError:
            return
        except (OSError, ValueError) as failure:
            self.load_error = f"settings ignored ({self.path}): {failure}"
            return
        if isinstance(stored, dict):
            self.data = _normalise(_merge(DEFAULTS, stored))       # keys of older versions are dropped

    def save(self):
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix(".tmp")
            with open(temp, "w", encoding="utf-8") as handle:
                json.dump(self.data, handle, indent=2, ensure_ascii=False)
                handle.write("\n")
            try:
                os.chmod(temp, 0o600)                  # may hold a model server token
            except OSError:
                pass
            os.replace(temp, self.path)

    def update(self, changes: dict):
        self.data = _normalise(_merge(self.data, changes))
        self.save()

    def __getitem__(self, key):
        return self.data[key]

    def public(self) -> dict:
        """For the UI: secrets replaced by a flag."""
        out = copy.deepcopy(self.data)
        for section, key in SECRET_KEYS:
            out[section][key + "_set"] = bool(out[section].pop(key))
        return out
