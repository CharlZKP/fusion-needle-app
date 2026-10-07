# center_hole {diameter, depth?, face}
# Hole at the centre of the bounding box of the named face (for a rectangle or
# a disc that is the centre of the face). No depth -> through hole. Fails when
# that point is not on the face (a ring, an L shape).
def run(_context: str):
    root = _root()
    body = _last_body(root)
    face = _face(body, ARGS["face"])
    span = _face_span(face, ARGS["face"])
    _axis, _level, (_ua, u_low, u_high), (_va, v_low, v_high) = span
    position = _span_point(span, (u_low + u_high) / 2.0, (v_low + v_high) / 2.0)
    if not _on_face(face, position):
        raise RuntimeError("the centre of the " + ARGS["face"] + " face is not on the face")
    holes = root.features.holeFeatures
    hole_input = holes.createSimpleInput(_vi(ARGS["diameter"] * MM))
    hole_input.setPositionByPoint(face, position)
    _hole_depth(hole_input)
    feature = holes.add(hole_input)
    _report("center_hole", feature.name,
            at_mm=[round(value / MM, 4) for value in _xyz(position)])
