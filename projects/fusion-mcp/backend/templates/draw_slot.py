# draw_slot {length, width, center_x, center_y, angle}
# Built from two arcs and two lines sharing their end points (arcs first, so
# the slot occupies four entity numbers: arc, arc, line, line).
def run(_context: str):
    sketch = _last_sketch(_root())
    half = ARGS["length"] * MM / 2.0
    radius = ARGS["width"] * MM / 2.0
    angle = math.radians(ARGS["angle"])
    cx, cy = ARGS["center_x"] * MM, ARGS["center_y"] * MM
    ux, uy = math.cos(angle), math.sin(angle)      # slot direction
    nx, ny = -uy, ux                               # left normal

    def point(along, across):
        return adsk.core.Point3D.create(cx + ux * along + nx * across, cy + uy * along + ny * across, 0)

    arcs = sketch.sketchCurves.sketchArcs
    lines = sketch.sketchCurves.sketchLines
    # counter-clockwise half circles: right end from -n to +n, left end from +n to -n
    right = arcs.addByCenterStartSweep(point(half, 0), point(half, -radius), math.pi)
    left = arcs.addByCenterStartSweep(point(-half, 0), point(-half, radius), math.pi)
    lines.addByTwoPoints(right.endSketchPoint, left.startSketchPoint)
    lines.addByTwoPoints(left.endSketchPoint, right.startSketchPoint)
    _report("draw_slot", sketch.name, sketch=sketch.name, curves=sketch.sketchCurves.count)
