# draw_line {start_x, start_y, end_x, end_y}  (sketch coordinates, mm)
def run(_context: str):
    sketch = _last_sketch(_root())
    start = adsk.core.Point3D.create(ARGS["start_x"] * MM, ARGS["start_y"] * MM, 0)
    end = adsk.core.Point3D.create(ARGS["end_x"] * MM, ARGS["end_y"] * MM, 0)
    if start.isEqualTo(end):
        raise ValueError("the line has zero length")
    sketch.sketchCurves.sketchLines.addByTwoPoints(start, end)
    _report("draw_line", sketch.name, sketch=sketch.name, curves=sketch.sketchCurves.count)
