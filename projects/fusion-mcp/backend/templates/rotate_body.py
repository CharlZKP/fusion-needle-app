# rotate_body {angle, axis}
# Move feature that rotates the last body in place: about a line parallel to
# the model axis through the centre of the body's bounding box. Positive
# angles are counter-clockwise seen from the positive end of the axis.
def run(_context: str):
    root = _root()
    body = _last_body(root)
    if ARGS["angle"] % 360 == 0:
        raise ValueError("rotate_body needs an angle that is not a full turn")
    low, high = _box_span(body.boundingBox)
    centre = adsk.core.Point3D.create((low[0] + high[0]) / 2.0, (low[1] + high[1]) / 2.0,
                                      (low[2] + high[2]) / 2.0)
    direction = {"x": (1, 0, 0), "y": (0, 1, 0), "z": (0, 0, 1)}[ARGS["axis"]]
    transform = adsk.core.Matrix3D.create()
    transform.setToRotation(math.radians(ARGS["angle"]),
                            adsk.core.Vector3D.create(direction[0], direction[1], direction[2]), centre)
    bodies = adsk.core.ObjectCollection.create()
    bodies.add(body)
    moves = root.features.moveFeatures
    move_input = moves.createInput2(bodies)
    move_input.defineAsFreeMove(transform)
    feature = moves.add(move_input)
    _report("rotate_body", feature.name, body=body.name)
