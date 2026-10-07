# Shared helpers, prepended to every rendered script (after the ARGS header).
# Fusion works in centimetres and radians internally; the tools take mm and
# degrees, so every length goes through MM and every angle through math.radians.
import json
import math

import adsk.core
import adsk.fusion

MM = 0.1      # mm -> cm
TOL = 1e-5    # cm


def _design():
    app = adsk.core.Application.get()
    design = adsk.fusion.Design.cast(app.activeProduct)
    if design is None:
        raise RuntimeError("no active Fusion design (switch to the Design workspace)")
    return design


def _root():
    return _design().rootComponent


def _vi(value):
    return adsk.core.ValueInput.createByReal(value)


def _body_names(root):
    return [root.bRepBodies.item(i).name for i in range(root.bRepBodies.count)]


def _report(tool, feature=None, **extra):
    root = _root()
    out = {"tool": tool, "feature": feature, "bodies": _body_names(root)}
    out.update(extra)
    print(json.dumps(out))


def _last_sketch(root):
    """Active sketch = the last sketch of the root component."""
    if root.sketches.count == 0:
        raise RuntimeError("no sketch in the design: call create_sketch first")
    return root.sketches.item(root.sketches.count - 1)


def _last_body(root):
    """Body = the last solid body of the root component."""
    if root.bRepBodies.count == 0:
        raise RuntimeError("no body in the design")
    return root.bRepBodies.item(root.bRepBodies.count - 1)


def _last_feature(design):
    """target last_feature = the last timeline item that is a feature."""
    timeline = design.timeline
    for index in range(timeline.count - 1, -1, -1):
        item = timeline.item(index)
        if item.isGroup:
            continue
        feature = adsk.fusion.Feature.cast(item.entity)
        if feature is not None:
            return feature
    raise RuntimeError("no feature in the timeline to repeat")


def _origin_plane(root, key):
    return {"xy": root.xYConstructionPlane, "xz": root.xZConstructionPlane,
            "yz": root.yZConstructionPlane}[key]


def _origin_axis(root, key):
    return {"x": root.xConstructionAxis, "y": root.yConstructionAxis,
            "z": root.zConstructionAxis}[key]


# face name -> (axis index, sign of the outward normal); Z is up, front looks at -Y.
_FACE_DIRS = {"top": (2, 1.0), "bottom": (2, -1.0), "front": (1, -1.0),
              "back": (1, 1.0), "left": (0, -1.0), "right": (0, 1.0)}


def _xyz(obj):
    return (obj.x, obj.y, obj.z)


# Index of the up axis, taken from the face table so there is one place to
# change if "up" ever has to follow a Y-up document (wrapper v3 tools use it).
_UP = _FACE_DIRS["top"][0]


def _face(body, which):
    """Planar face at the bounding-box extreme whose outward normal points that way."""
    axis, sign = _FACE_DIRS[which]
    box = body.boundingBox
    extreme = _xyz(box.maxPoint)[axis] if sign > 0 else _xyz(box.minPoint)[axis]
    best = None
    for face in body.faces:
        if face.geometry.surfaceType != adsk.core.SurfaceTypes.PlaneSurfaceType:
            continue
        point = face.pointOnFace
        ok, normal = face.evaluator.getNormalAtPoint(point)
        if not ok:
            continue
        if _xyz(normal)[axis] * sign < 0.999:
            continue
        if abs(_xyz(point)[axis] - extreme) > TOL:
            continue
        if best is None or face.area > best.area:
            best = face
    if best is None:
        raise RuntimeError("the body has no flat " + which + " face")
    return best


def _edge_points(edge):
    """Start, middle and end point of an edge."""
    evaluator = edge.evaluator
    ok, start, end = evaluator.getParameterExtents()
    if not ok:
        raise RuntimeError("cannot evaluate an edge")
    points = []
    for parameter in (start, (start + end) / 2.0, end):
        ok, point = evaluator.getPointAtParameter(parameter)
        if not ok:
            raise RuntimeError("cannot evaluate an edge")
        points.append(point)
    return points


def _is_inner_loop_edge(edge):
    """True for an edge that bounds a hole in a flat face (e.g. the rim of a drilled hole)."""
    for co_edge in edge.coEdges:
        loop = co_edge.loop
        if not loop.isOuter and \
                loop.face.geometry.surfaceType == adsk.core.SurfaceTypes.PlaneSurfaceType:
            return True
    return False


def _edges(body, which):
    """Edge set by position: all | top | bottom | vertical.

    top / bottom: edges lying in the plane of the bounding-box top / bottom,
    without the rims of holes and pockets. vertical: straight edges parallel to Z.
    """
    box = body.boundingBox
    found = adsk.core.ObjectCollection.create()
    for edge in body.edges:
        if which == "all":
            found.add(edge)
            continue
        points = _edge_points(edge)
        if which == "top":
            keep = all(abs(p.z - box.maxPoint.z) <= TOL for p in points) \
                and not _is_inner_loop_edge(edge)
        elif which == "bottom":
            keep = all(abs(p.z - box.minPoint.z) <= TOL for p in points) \
                and not _is_inner_loop_edge(edge)
        else:
            straight = edge.geometry.curveType == adsk.core.Curve3DTypes.Line3DCurveType
            keep = straight and abs(points[0].x - points[2].x) <= TOL \
                and abs(points[0].y - points[2].y) <= TOL \
                and abs(points[0].z - points[2].z) > TOL
        if keep:
            found.add(edge)
    if found.count == 0:
        raise RuntimeError("no " + which + " edges found on the body")
    return found


def _curve(sketch, number):
    """Sketch entity N = the N-th curve of the sketch in creation order (1-based)."""
    curves = sketch.sketchCurves
    if number < 1 or number > curves.count:
        raise RuntimeError("the sketch has no entity " + str(number)
                           + " (it has " + str(curves.count) + " curves)")
    return curves.item(number - 1)


def _anchor(curve):
    """Reference point of a curve: centre of a circle / arc, start point of a line."""
    circle = adsk.fusion.SketchCircle.cast(curve)
    if circle is not None:
        return circle.centerSketchPoint
    arc = adsk.fusion.SketchArc.cast(curve)
    if arc is not None:
        return arc.centerSketchPoint
    line = adsk.fusion.SketchLine.cast(curve)
    if line is not None:
        return line.startSketchPoint
    raise RuntimeError("unsupported sketch entity type")


def _line(curve, number):
    line = adsk.fusion.SketchLine.cast(curve)
    if line is None:
        raise RuntimeError("entity " + str(number) + " is not a line")
    return line


def _pattern_entities(design, root, target):
    entities = adsk.core.ObjectCollection.create()
    if target == "body":
        entities.add(_last_body(root))
    else:
        entities.add(_last_feature(design))
    return entities


# ---- wrapper v3: geometry read from the body at run time ---------------------
def _box_span(box):
    """(low, high) corner of a bounding box as tuples, in cm."""
    return _xyz(box.minPoint), _xyz(box.maxPoint)


def _face_span(face, which):
    """Where a named face lies: (axis, level, (u_axis, u_low, u_high), (v_axis, v_low, v_high)).

    axis / level: the axis the face is normal to and its coordinate on it.
    u, v: the two other model axes with the extent of the face's own bounding
    box along them. All in cm.
    """
    axis, _sign = _FACE_DIRS[which]
    low, high = _box_span(face.boundingBox)
    level = _xyz(face.pointOnFace)[axis]
    u_axis, v_axis = [index for index in (0, 1, 2) if index != axis]
    return axis, level, (u_axis, low[u_axis], high[u_axis]), (v_axis, low[v_axis], high[v_axis])


def _span_point(span, u, v):
    """Model point on the face plane at in-plane coordinates u, v (cm)."""
    axis, level, (u_axis, _ul, _uh), (v_axis, _vl, _vh) = span
    xyz = [0.0, 0.0, 0.0]
    xyz[axis], xyz[u_axis], xyz[v_axis] = level, u, v
    return adsk.core.Point3D.create(xyz[0], xyz[1], xyz[2])


def _on_face(face, point):
    """True when a model point lies on the face (inside its boundary, not in an opening)."""
    evaluator = face.evaluator
    ok, parameter = evaluator.getParameterAtPoint(point)
    return bool(ok) and bool(evaluator.isParameterOnFace(parameter))


def _corner_positions(u_low, u_high, v_low, v_high, margin):
    """Four (u, v) positions inset by margin from the corners of a rectangle."""
    if margin * 2.0 >= (u_high - u_low) - TOL or margin * 2.0 >= (v_high - v_low) - TOL:
        raise ValueError("the margin is too large for the face: the holes would meet or cross")
    return [(u_low + margin, v_low + margin), (u_high - margin, v_low + margin),
            (u_high - margin, v_high - margin), (u_low + margin, v_high - margin)]


def _hole_depth(hole_input):
    """Blind when the call carried a depth, through all otherwise."""
    if "depth" in GIVEN:
        hole_input.setDistanceExtent(_vi(ARGS["depth"] * MM))
    else:
        hole_input.setAllExtent(adsk.fusion.ExtentDirections.PositiveExtentDirection)


def _split_axis(low, high, direction):
    """Model axis the cut plane is normal to.

    horizontal: the up axis (top and bottom halves). vertical: the longer of the
    two horizontal axes (a cut across the longest side). lengthwise: the shorter
    one (a cut along the longest side). Equal sides count the first axis as long.
    """
    if direction == "horizontal":
        return _UP
    first, second = [index for index in (0, 1, 2) if index != _UP]
    longer_first = (high[first] - low[first]) >= (high[second] - low[second])
    if direction == "vertical":
        return first if longer_first else second
    return second if longer_first else first


def _new_body_from_sketch(root, sketch, height):
    """Extrude the only profile of a fresh sketch into a new body (height in cm)."""
    if sketch.profiles.count == 0:
        raise RuntimeError("the outline did not give a closed profile")
    extrudes = root.features.extrudeFeatures
    extrude_input = extrudes.createInput(sketch.profiles.item(0),
                                         adsk.fusion.FeatureOperations.NewBodyFeatureOperation)
    extrude_input.setDistanceExtent(False, _vi(height))
    return extrudes.add(extrude_input)
