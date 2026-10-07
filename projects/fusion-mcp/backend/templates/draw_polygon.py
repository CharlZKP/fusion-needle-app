# draw_polygon {shape, size, measure, center_x, center_y, angle}
# N lines sharing their end points, so the polygon occupies N entity numbers.
# measure: flats = diameter of the inscribed circle (across flats; for an odd
# number of sides twice the apothem), corners = diameter of the circumscribed
# circle, side = length of one side. angle 0 puts the first corner on +X.
_SIDES = {"triangle": 3, "pentagon": 5, "hexagon": 6, "octagon": 8}


def run(_context: str):
    sketch = _last_sketch(_root())
    count = _SIDES[ARGS["shape"]]
    size = ARGS["size"] * MM
    if ARGS["measure"] == "corners":
        radius = size / 2.0
    elif ARGS["measure"] == "side":
        radius = size / (2.0 * math.sin(math.pi / count))
    else:
        radius = size / (2.0 * math.cos(math.pi / count))
    cx, cy = ARGS["center_x"] * MM, ARGS["center_y"] * MM
    turn = math.radians(ARGS["angle"])

    def corner(index):
        at = turn + 2.0 * math.pi * index / count
        return adsk.core.Point3D.create(cx + radius * math.cos(at), cy + radius * math.sin(at), 0)

    lines = sketch.sketchCurves.sketchLines
    first = lines.addByTwoPoints(corner(0), corner(1))
    previous = first
    for index in range(2, count):
        previous = lines.addByTwoPoints(previous.endSketchPoint, corner(index))
    lines.addByTwoPoints(previous.endSketchPoint, first.startSketchPoint)
    _report("draw_polygon", sketch.name, sketch=sketch.name, curves=sketch.sketchCurves.count)
