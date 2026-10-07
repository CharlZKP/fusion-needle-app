# add_constraint {kind, entity_one?, entity_two?}
# Entity numbers count the curves of the active sketch in creation order.
# Without entity_one the last curve is used. A "point" of an entity is the
# centre of a circle / arc or the start point of a line.
def run(_context: str):
    sketch = _last_sketch(_root())
    constraints = sketch.geometricConstraints
    kind = ARGS["kind"]
    number_one = ARGS["entity_one"] if "entity_one" in GIVEN else sketch.sketchCurves.count
    one = _curve(sketch, number_one)
    two = _curve(sketch, ARGS["entity_two"]) if "entity_two" in GIVEN else None
    if two is None and kind not in ("horizontal", "vertical", "fix"):
        raise ValueError("a " + kind + " constraint needs two entities")

    if kind == "horizontal":
        constraints.addHorizontal(_line(one, number_one))
    elif kind == "vertical":
        constraints.addVertical(_line(one, number_one))
    elif kind == "fix":
        one.isFixed = True
    elif kind == "parallel":
        constraints.addParallel(_line(one, number_one), _line(two, ARGS["entity_two"]))
    elif kind == "perpendicular":
        constraints.addPerpendicular(_line(one, number_one), _line(two, ARGS["entity_two"]))
    elif kind == "tangent":
        constraints.addTangent(one, two)
    elif kind == "equal":
        constraints.addEqual(one, two)
    elif kind == "concentric":
        constraints.addConcentric(one, two)
    elif kind == "coincident":
        constraints.addCoincident(_anchor(one), two)
    elif kind == "midpoint":
        constraints.addMidPoint(_anchor(one), _line(two, ARGS["entity_two"]))
    else:  # symmetric: about the first construction line of the sketch
        axis = None
        lines = sketch.sketchCurves.sketchLines
        for index in range(lines.count):
            if lines.item(index).isConstruction:
                axis = lines.item(index)
                break
        if axis is None:
            raise ValueError("a symmetric constraint needs a construction line in the sketch")
        constraints.addSymmetry(one, two, axis)
    _report("add_constraint", sketch.name, sketch=sketch.name, constraints=constraints.count)
