# draw_arc {radius, start_angle, end_angle, center_x, center_y}
# Counter-clockwise from start_angle to end_angle, angles measured from +X of
# the sketch. One entity number.
def run(_context: str):
    sketch = _last_sketch(_root())
    radius = ARGS["radius"] * MM
    cx, cy = ARGS["center_x"] * MM, ARGS["center_y"] * MM
    sweep = (ARGS["end_angle"] - ARGS["start_angle"]) % 360.0
    if sweep == 0:
        raise ValueError("the arc's start and end angle coincide (use draw_circle for a full circle)")
    start = math.radians(ARGS["start_angle"])
    centre = adsk.core.Point3D.create(cx, cy, 0)
    begin = adsk.core.Point3D.create(cx + radius * math.cos(start), cy + radius * math.sin(start), 0)
    sketch.sketchCurves.sketchArcs.addByCenterStartSweep(centre, begin, math.radians(sweep))
    _report("draw_arc", sketch.name, sketch=sketch.name, curves=sketch.sketchCurves.count)
