# corner_holes {diameter, margin, depth?, face}
# Four holes, one near each corner of the named face, each inset by margin from
# both edges. The corners are those of the face's own bounding box, so the face
# has to be a rectangle (rounded corners are fine while the hole centres stay
# on the face). On any other outline at least one centre misses the face and
# the script fails before it changes anything.
# One Hole feature on four sketch points (a hidden sketch on the face), so the
# holes are edited, patterned and undone together. No depth -> through holes.
def run(_context: str):
    root = _root()
    body = _last_body(root)
    face = _face(body, ARGS["face"])
    span = _face_span(face, ARGS["face"])
    _axis, _level, (_ua, u_low, u_high), (_va, v_low, v_high) = span
    margin = ARGS["margin"] * MM
    positions = [_span_point(span, u, v)
                 for u, v in _corner_positions(u_low, u_high, v_low, v_high, margin)]
    for position in positions:
        if not _on_face(face, position):
            raise RuntimeError("the " + ARGS["face"] + " face is not a rectangle: a corner hole "
                               "would miss it (use create_hole with positions instead)")
    sketch = root.sketches.addWithoutEdges(face)
    points = adsk.core.ObjectCollection.create()
    for position in positions:
        points.add(sketch.sketchPoints.add(sketch.modelToSketchSpace(position)))
    holes = root.features.holeFeatures
    hole_input = holes.createSimpleInput(_vi(ARGS["diameter"] * MM))
    hole_input.setPositionBySketchPoints(points)
    _hole_depth(hole_input)
    feature = holes.add(hole_input)
    sketch.isLightBulbOn = False
    _report("corner_holes", feature.name, holes=len(positions),
            at_mm=[[round(value / MM, 4) for value in _xyz(position)] for position in positions])
