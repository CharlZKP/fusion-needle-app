# PyInstaller spec for Fusion Needle: one folder, one executable.
#
#   pyinstaller --noconfirm --distpath dist --workpath build packaging/fusion_needle.spec
#
# Run it with a Python 3.12+ that can import stepserver, needle and yaml (the
# repo's .venv, or an environment that has its site-packages on the path; see
# build.sh / build_windows.ps1). PyInstaller does not cross-compile: build on the
# OS you ship for.
#
# What goes in:
#   app/                      this app (frozen) and app/ui/ (the page and examples.json)
#   stepserver, needle               the model server; the exe re-runs itself as
#                             `-m stepserver.cli serve`, `-m stepserver.engine` and
#                             `needle/_worker.py --child` (see fusion_needle.py)
#   projects/<project>/       project.yaml, tools/, backend/ (templates, renderer, router)
# Environment:
#   FN_PROJECT   project to bundle (default fusion-mcp)
#   FN_CONSOLE   1 = keep a console window (default: none on Windows)
import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

DESKTOP = Path(SPECPATH).resolve().parent
ROOT = DESKTOP
PROJECT = os.environ.get("FN_PROJECT", "fusion-mcp")
PROJECT_DIR = ROOT / "projects" / PROJECT

datas = [(str(DESKTOP / "app" / "ui"), "app/ui")]
for relative in ("project.yaml", "tools", "backend"):
    source = PROJECT_DIR / relative
    if not source.exists():
        raise SystemExit(f"missing {source}")
    target = f"projects/{PROJECT}/{relative}" if source.is_dir() else f"projects/{PROJECT}"
    datas.append((str(source), target))
# needle is collected as source files (module_collection_mode below): it starts
# its tuned-model worker as `<exe> <path>/needle/_worker.py --child`, so the file
# must exist on disk. Its tokenizer model is data.
datas += collect_data_files("needle", excludes=["playground/*", "**/__pycache__/*"])
for distribution in ("cactus-needle", "huggingface_hub"):
    try:
        datas += copy_metadata(distribution)
    except Exception:
        pass

hiddenimports = ["stepserver.cli", "stepserver.engine", "stepserver.serve", "yaml", "huggingface_hub"]
hiddenimports += collect_submodules("needle", filter=lambda name: ".environments" not in name
                                    and ".playground" not in name)

# Training-only and unrelated packages: the app only runs inference through the engine.
excludes = ["jax", "jaxlib", "flax", "optax", "orbax", "tensorflow", "torch", "matplotlib", "tkinter",
            "IPython", "pytest", "PyInstaller"]

analysis = Analysis(
    [str(DESKTOP / "fusion_needle.py")],
    pathex=[str(DESKTOP)],
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=excludes,
    module_collection_mode={"needle": "py"},
    noarchive=False,
)
pyz = PYZ(analysis.pure)
console = os.environ.get("FN_CONSOLE", "") == "1" or os.name != "nt"
exe = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="FusionNeedle",
    console=console,
    icon=None,
)
COLLECT(exe, analysis.binaries, analysis.datas, name="FusionNeedle")
