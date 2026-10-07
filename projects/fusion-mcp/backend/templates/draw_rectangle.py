# draw_rectangle {width, height, center_x, center_y}  (sketch coordinates, mm)
def run(_context: str):
    sketch = _last_sketch(_root())
    cx, cy = ARGS["center_x"] * MM, ARGS["center_y"] * MM
    centre = adsk.core.Point3D.create(cx, cy, 0)
    corner = adsk.core.Point3D.create(cx + ARGS["width"] * MM / 2.0, cy + ARGS["height"] * MM / 2.0, 0)
    sketch.sketchCurves.sketchLines.addCenterPointRectangle(centre, corner)
    _report("draw_rectangle", sketch.name, sketch=sketch.name, curves=sketch.sketchCurves.count)
