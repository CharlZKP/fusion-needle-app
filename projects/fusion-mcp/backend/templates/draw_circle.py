# draw_circle {diameter, center_x, center_y}  (sketch coordinates, mm)
def run(_context: str):
    sketch = _last_sketch(_root())
    centre = adsk.core.Point3D.create(ARGS["center_x"] * MM, ARGS["center_y"] * MM, 0)
    sketch.sketchCurves.sketchCircles.addByCenterRadius(centre, ARGS["diameter"] * MM / 2.0)
    _report("draw_circle", sketch.name, sketch=sketch.name, curves=sketch.sketchCurves.count)
