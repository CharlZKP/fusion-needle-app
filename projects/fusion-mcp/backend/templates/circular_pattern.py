# circular_pattern {count, axis, angle, target}
def run(_context: str):
    design = _design()
    root = design.rootComponent
    entities = _pattern_entities(design, root, ARGS["target"])
    patterns = root.features.circularPatternFeatures
    pattern_input = patterns.createInput(entities, _origin_axis(root, ARGS["axis"]))
    pattern_input.quantity = _vi(ARGS["count"])
    pattern_input.totalAngle = _vi(math.radians(ARGS["angle"]))
    pattern_input.isSymmetric = False
    feature = patterns.add(pattern_input)
    _report("circular_pattern", feature.name)
