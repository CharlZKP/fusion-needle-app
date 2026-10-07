# rectangular_pattern {x_count, x_spacing, y_count, y_spacing?, target}
# Spacing is the distance between neighbouring instances.
def run(_context: str):
    design = _design()
    root = design.rootComponent
    entities = _pattern_entities(design, root, ARGS["target"])
    if ARGS["y_count"] > 1 and "y_spacing" not in GIVEN:
        raise ValueError("y_count > 1 needs y_spacing")
    patterns = root.features.rectangularPatternFeatures
    pattern_input = patterns.createInput(
        entities, root.xConstructionAxis, _vi(ARGS["x_count"]), _vi(ARGS["x_spacing"] * MM),
        adsk.fusion.PatternDistanceType.SpacingPatternDistanceType)
    if ARGS["y_count"] > 1:
        pattern_input.setDirectionTwo(root.yConstructionAxis, _vi(ARGS["y_count"]),
                                      _vi(ARGS["y_spacing"] * MM))
    feature = patterns.add(pattern_input)
    _report("rectangular_pattern", feature.name)
