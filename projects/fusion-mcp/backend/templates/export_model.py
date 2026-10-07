# export_model {format, name?}
# Writes into EXPORT_DIR, a literal the app passed to the renderer; the model
# has no argument for it. Never overwrites: an existing file gets -2, -3, ...
import os

_EXTENSIONS = {"step": ".step", "stl": ".stl", "3mf": ".3mf", "iges": ".iges", "f3d": ".f3d"}


def run(_context: str):
    design = _design()
    root = design.rootComponent
    kind = ARGS["format"]
    stem = ARGS["name"] if "name" in GIVEN else "model"
    os.makedirs(EXPORT_DIR, exist_ok=True)
    path = os.path.join(EXPORT_DIR, stem + _EXTENSIONS[kind])
    number = 1
    while os.path.exists(path):
        number += 1
        if number > 999:
            raise RuntimeError("too many exports named " + stem)
        path = os.path.join(EXPORT_DIR, stem + "-" + str(number) + _EXTENSIONS[kind])
    manager = design.exportManager
    if kind == "step":
        options = manager.createSTEPExportOptions(path, root)
    elif kind == "stl":
        options = manager.createSTLExportOptions(root, path)
        options.meshRefinement = adsk.fusion.MeshRefinementSettings.MeshRefinementHigh
    elif kind == "3mf":
        options = manager.createC3MFExportOptions(root, path)
    elif kind == "iges":
        options = manager.createIGESExportOptions(path, root)
    else:
        options = manager.createFusionArchiveExportOptions(path, root)
    if not manager.execute(options):
        raise RuntimeError("Fusion reported that the " + kind + " export failed")
    _report("export_model", None, path=path, format=kind)
