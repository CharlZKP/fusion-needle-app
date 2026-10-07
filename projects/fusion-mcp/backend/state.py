"""Read the design state through a read-only script and turn it into the
``system`` facts of README.md:

    units: mm; sketch: Sketch1 on xy; bodies: Body1; last_feature: Extrude1

``state_call()`` is the MCP payload (``readOnly: true``, so it also runs while
a command dialog is open). ``parse_state(output)`` takes the tool answer (or
just the printed text) and returns the string. Definitions:

* sketch       - the last timeline item when it is a sketch (not yet used by a
                 feature); ``on xy|xz|yz``, ``on <top|...> face`` or ``on plane``.
* bodies       - solid bodies of the root component, in order.
* last_feature - the last timeline item that is a feature.

Standard library only.
"""
from __future__ import annotations

import json

import mcp_result

STATE_SCRIPT = r'''
import json

import adsk.core
import adsk.fusion

TOL = 1e-5
FACE_DIRS = {"top": (2, 1.0), "bottom": (2, -1.0), "front": (1, -1.0),
             "back": (1, 1.0), "left": (0, -1.0), "right": (0, 1.0)}


def _xyz(obj):
    return (obj.x, obj.y, obj.z)


def _face_name(face):
    if face.geometry.surfaceType != adsk.core.SurfaceTypes.PlaneSurfaceType:
        return "face"
    ok, normal = face.evaluator.getNormalAtPoint(face.pointOnFace)
    if not ok:
        return "face"
    box = face.body.boundingBox
    point = face.pointOnFace
    for name, (axis, sign) in FACE_DIRS.items():
        extreme = _xyz(box.maxPoint)[axis] if sign > 0 else _xyz(box.minPoint)[axis]
        if _xyz(normal)[axis] * sign > 0.999 and abs(_xyz(point)[axis] - extreme) <= TOL:
            return name + " face"
    return "face"


def _sketch_on(root, sketch):
    reference = sketch.referencePlane
    face = adsk.fusion.BRepFace.cast(reference)
    if face is not None:
        return _face_name(face)
    if reference == root.xYConstructionPlane:
        return "xy"
    if reference == root.xZConstructionPlane:
        return "xz"
    if reference == root.yZConstructionPlane:
        return "yz"
    return "plane"


def run(_context: str):
    app = adsk.core.Application.get()
    design = adsk.fusion.Design.cast(app.activeProduct)
    if design is None:
        raise RuntimeError("no active Fusion design")
    root = design.rootComponent
    bodies = []
    for index in range(root.bRepBodies.count):
        body = root.bRepBodies.item(index)
        if body.isSolid:
            bodies.append(body.name)

    sketch = None
    last_feature = None
    unhealthy = []
    timeline = design.timeline
    last_index = timeline.markerPosition - 1
    for index in range(last_index, -1, -1):
        item = timeline.item(index)
        if item.isGroup:
            continue
        if item.healthState != adsk.fusion.FeatureHealthStates.HealthyFeatureHealthState:
            unhealthy.append({"name": item.name, "message": item.errorOrWarningMessage})
        entity = item.entity
        if index == last_index:
            as_sketch = adsk.fusion.Sketch.cast(entity)
            if as_sketch is not None:
                sketch = {"name": as_sketch.name, "on": _sketch_on(root, as_sketch)}
        if last_feature is None and adsk.fusion.Feature.cast(entity) is not None:
            last_feature = item.name
    print(json.dumps({"state": 1, "units": "mm", "sketch": sketch, "bodies": bodies,
                      "last_feature": last_feature, "unhealthy": unhealthy}))
'''.lstrip()


def state_call() -> dict:
    """``tools/call`` params that read the state without modifying the design."""
    return {"name": "fusion_mcp_execute",
            "arguments": {"featureType": "script",
                          "object": {"script": STATE_SCRIPT, "readOnly": True}}}


def format_system(sketch, bodies, last_feature) -> str:
    """``sketch`` is None or {"name", "on"}."""
    sketch_txt = f"{sketch['name']} on {sketch['on']}" if sketch else "none"
    bodies_txt = ", ".join(bodies) if bodies else "none"
    return (f"units: mm; sketch: {sketch_txt}; bodies: {bodies_txt}; "
            f"last_feature: {last_feature or 'none'}")


def parse_state_dict(output) -> dict:
    """The JSON object the state script printed (last matching line wins).

    ``output`` is anything mcp_result understands: the JSON-RPC response, its
    result, the content list, or the printed text. The server wraps what a
    script printed as ``{"message": "<printed text>", "success": true}`` inside
    the first text block; a failed call raises ValueError with its error.
    """
    parsed = mcp_result.parse_result(output)
    if not parsed["ok"]:
        raise ValueError(f"the state script failed: {parsed['error']}")
    for line in reversed(parsed["message"].splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if isinstance(data, dict) and "bodies" in data and "last_feature" in data:
            return data
    raise ValueError("no state line found in the tool output")


def parse_state(output) -> str:
    """Tool output of ``state_call()`` -> the ``system`` facts string."""
    data = parse_state_dict(output)
    sketch = data.get("sketch")
    if sketch is not None and not (isinstance(sketch, dict) and "name" in sketch and "on" in sketch):
        raise ValueError("malformed sketch entry in the state")
    bodies = data.get("bodies") or []
    if not all(isinstance(name, str) for name in bodies):
        raise ValueError("malformed bodies entry in the state")
    return format_system(sketch, list(bodies), data.get("last_feature"))
