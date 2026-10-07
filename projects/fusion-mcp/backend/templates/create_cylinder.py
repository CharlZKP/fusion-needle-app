# create_cylinder {diameter, height}
# One-step primitive, the same result as create_sketch {} + draw_circle +
# extrude: a circle on the XY origin plane centred on the origin, extruded by
# height along +Z.
def run(_context: str):
    root = _root()
    sketch = root.sketches.add(root.xYConstructionPlane)
    centre = adsk.core.Point3D.create(0, 0, 0)
    sketch.sketchCurves.sketchCircles.addByCenterRadius(centre, ARGS["diameter"] * MM / 2.0)
    feature = _new_body_from_sketch(root, sketch, ARGS["height"] * MM)
    _report("create_cylinder", feature.name, body=feature.bodies.item(0).name)
