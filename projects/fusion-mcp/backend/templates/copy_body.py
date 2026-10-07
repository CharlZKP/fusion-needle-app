# copy_body {count, spacing?, axis}
# Adds count copies of the last body in a row along a model axis (one
# Rectangular Pattern feature of the body with count + 1 instances).
# spacing is the distance from one copy to the next. Not stated: the body's
# size along the axis plus a gap of a quarter of that size (at least 2 mm), so
# the copies sit side by side without touching.
def run(_context: str):
    root = _root()
    body = _last_body(root)
    index = "xyz".index(ARGS["axis"])
    low, high = _box_span(body.boundingBox)
    size = high[index] - low[index]
    if "spacing" in GIVEN:
        pitch = ARGS["spacing"] * MM
    else:
        pitch = size + max(size * 0.25, 2.0 * MM)
    entities = adsk.core.ObjectCollection.create()
    entities.add(body)
    patterns = root.features.rectangularPatternFeatures
    pattern_input = patterns.createInput(
        entities, _origin_axis(root, ARGS["axis"]), _vi(ARGS["count"] + 1), _vi(pitch),
        adsk.fusion.PatternDistanceType.SpacingPatternDistanceType)
    feature = patterns.add(pattern_input)
    _report("copy_body", feature.name, copies=ARGS["count"], spacing_mm=round(pitch / MM, 4))
