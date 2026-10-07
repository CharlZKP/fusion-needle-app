# create_box {width, depth, height}
# One-step primitive, the same result as create_sketch {} + draw_rectangle +
# extrude: a sketch on the XY origin plane with the rectangle centred on the
# origin (width along X, depth along Y), extruded by height along +Z.
def run(_context: str):
    root = _root()
    sketch = root.sketches.add(root.xYConstructionPlane)
    centre = adsk.core.Point3D.create(0, 0, 0)
    corner = adsk.core.Point3D.create(ARGS["width"] * MM / 2.0, ARGS["depth"] * MM / 2.0, 0)
    sketch.sketchCurves.sketchLines.addCenterPointRectangle(centre, corner)
    feature = _new_body_from_sketch(root, sketch, ARGS["height"] * MM)
    _report("create_box", feature.name, body=feature.bodies.item(0).name)
