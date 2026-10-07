# move_body {x, y, z}
# Translates the last body along the model axes (a Move feature in the timeline).
def run(_context: str):
    root = _root()
    body = _last_body(root)
    dx, dy, dz = ARGS["x"] * MM, ARGS["y"] * MM, ARGS["z"] * MM
    if dx == 0 and dy == 0 and dz == 0:
        raise ValueError("move_body needs a distance along at least one axis")
    bodies = adsk.core.ObjectCollection.create()
    bodies.add(body)
    moves = root.features.moveFeatures
    move_input = moves.createInput2(bodies)
    move_input.defineAsTranslateXYZ(_vi(dx), _vi(dy), _vi(dz), True)      # True: design space
    feature = moves.add(move_input)
    _report("move_body", feature.name, body=body.name)
