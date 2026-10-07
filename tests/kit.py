"""Shared values and helpers of the app's tests.

Kept out of conftest.py and imported relatively (``from .kit import ...``): the
repo has other test folders whose test files do ``from conftest import ...``,
and a module that is importable as plain ``conftest`` (or that puts this folder
first on sys.path) would answer those imports when the folders are collected in
one pytest run. This folder is a package for the same reason.
"""
import atexit
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

TESTS = Path(__file__).resolve().parent
DESKTOP = TESTS.parent
ROOT = DESKTOP
PROJECT = ROOT / "projects" / "fusion-mcp"
if str(DESKTOP) not in sys.path:
    sys.path.insert(0, str(DESKTOP))            # `import app`

# No test may read or write the real per-user config dir (settings, pid files, diagnostics).
if "FUSION_NEEDLE_CONFIG_DIR" not in os.environ:
    os.environ["FUSION_NEEDLE_CONFIG_DIR"] = tempfile.mkdtemp(prefix="fusion-needle-tests-")
    atexit.register(shutil.rmtree, os.environ["FUSION_NEEDLE_CONFIG_DIR"], ignore_errors=True)

PLATE = [{"name": "create_sketch", "arguments": {}},
         {"name": "draw_rectangle", "arguments": {"width": 40, "height": 30}},
         {"name": "extrude", "arguments": {"distance": 10}}]
HOLE = [{"name": "create_hole", "arguments": {"diameter": 6, "x": 10, "y": 0}}]
PATTERN = [{"name": "circular_pattern", "arguments": {"count": 6}}]
FILLET = [{"name": "fillet", "arguments": {"radius": 2, "edges": "top"}}]
GOAL = "plate 40x30x10 → Ø6 hole at (10, 0) -> polar pattern ×6 then fillet the top edges R2"
ANSWERS = {
    "plate 40x30x10": {"calls": PLATE, "reasoning": "'40x30' -> width, height; '10' -> distance"},
    "Ø6 hole at (10, 0)": {"calls": HOLE, "reasoning": "'Ø6' -> diameter; '(10, 0)' -> x, y"},
    "polar pattern ×6": {"calls": PATTERN, "reasoning": "'×6' -> count"},
    "fillet the top edges R2": {"calls": FILLET, "reasoning": "'R2' -> radius; 'top' -> edges"},
}


class Port:
    """What Session needs from a model, over a FakeModel (no HTTP)."""

    def __init__(self, fake):
        self.fake = fake

    def step(self, query, system, names):
        return self.fake.step(query, system, names)

    def catalogue(self):
        return self.fake.catalogue

    def max_tools(self):
        return 5

    def name(self):
        return "fake-20L.cact"


def read_rows(path):
    if not Path(path).is_file():
        return []
    with open(path, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def fake_python(folder) -> str:
    """An executable that stands in for the model server's interpreter and runs fake_serve.py.

    Used as the ``model.python`` setting, so the app starts it exactly as it
    starts `python -m stepserver.cli serve ...`.
    """
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    script = TESTS / "fake_serve.py"
    if sys.platform == "win32":
        path = folder / "fakepy.cmd"
        path.write_text(f'@"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
    else:
        path = folder / "fakepy"
        path.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8")
        path.chmod(0o755)
    return str(path)
