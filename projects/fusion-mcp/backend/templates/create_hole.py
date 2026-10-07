# create_hole {diameter, x, y, depth?, face}
# x, y are model coordinates in the plane of the face:
#   top / bottom -> (X, Y);  front / back -> (X, Z);  left / right -> (Y, Z).
# No depth -> through hole.
def run(_context: str):
    root = _root()
    body = _last_body(root)
    face = _face(body, ARGS["face"])
    axis, _sign = _FACE_DIRS[ARGS["face"]]
    level = _xyz(face.pointOnFace)[axis]
    x, y = ARGS["x"] * MM, ARGS["y"] * MM
    if axis == 2:
        position = adsk.core.Point3D.create(x, y, level)
    elif axis == 1:
        position = adsk.core.Point3D.create(x, level, y)
    else:
        position = adsk.core.Point3D.create(level, x, y)

    holes = root.features.holeFeatures
    hole_input = holes.createSimpleInput(_vi(ARGS["diameter"] * MM))
    hole_input.setPositionByPoint(face, position)
    if "depth" in GIVEN:
        hole_input.setDistanceExtent(_vi(ARGS["depth"] * MM))
    else:
        hole_input.setAllExtent(adsk.fusion.ExtentDirections.PositiveExtentDirection)
    feature = holes.add(hole_input)
    _report("create_hole", feature.name)
