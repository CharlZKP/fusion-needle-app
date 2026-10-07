# add_dimension {kind, value, entity_one?, entity_two?}
# Entity numbers count the curves of the active sketch in creation order.
# Without entity_one the last curve is used.
def run(_context: str):
    sketch = _last_sketch(_root())
    dims = sketch.sketchDimensions
    kind = ARGS["kind"]
    number_one = ARGS["entity_one"] if "entity_one" in GIVEN else sketch.sketchCurves.count
    one = _curve(sketch, number_one)
    two = _curve(sketch, ARGS["entity_two"]) if "entity_two" in GIVEN else None
    box = sketch.boundingBox
    text = adsk.core.Point3D.create(box.maxPoint.x + 1.0, box.maxPoint.y + 1.0, 0)
    orientations = adsk.fusion.DimensionOrientations

    if kind == "diameter":
        dim = dims.addDiameterDimension(one, text)
    elif kind == "radius":
        dim = dims.addRadialDimension(one, text)
    elif kind == "angle":
        if two is None:
            raise ValueError("an angle dimension needs two lines")
        dim = dims.addAngularDimension(_line(one, number_one), _line(two, ARGS["entity_two"]), text)
    else:
        orientation = {"distance": orientations.AlignedDimensionOrientation,
                       "horizontal": orientations.HorizontalDimensionOrientation,
                       "vertical": orientations.VerticalDimensionOrientation}[kind]
        if two is None:
            line = _line(one, number_one)
            dim = dims.addDistanceDimension(line.startSketchPoint, line.endSketchPoint, orientation, text)
        elif kind == "distance" and adsk.fusion.SketchLine.cast(one) is not None:
            # line to parallel line / point: perpendicular offset
            other = two if adsk.fusion.SketchLine.cast(two) is not None else _anchor(two)
            dim = dims.addOffsetDimension(one, other, text)
        else:
            dim = dims.addDistanceDimension(_anchor(one), _anchor(two), orientation, text)

    if kind == "angle":
        dim.parameter.value = math.radians(ARGS["value"])
    else:
        dim.parameter.value = ARGS["value"] * MM
    _report("add_dimension", sketch.name, sketch=sketch.name, dimension=dim.parameter.name)
