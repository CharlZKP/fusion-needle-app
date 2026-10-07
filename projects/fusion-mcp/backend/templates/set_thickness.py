# set_thickness {thickness}
# Edits an existing extrude instead of adding a feature: the most recent
# extrude of the timeline that made a new body (the plate itself, not a pocket
# or boss on it); when there is none, the most recent extrude. Its distance
# becomes the new thickness and keeps its direction. A symmetric extrude keeps
# being symmetric with the new total thickness. An extrude without a distance
# (through all, to object) is an error.
def _extrude_to_edit(design):
    timeline = design.timeline
    fallback = None
    for index in range(timeline.count - 1, -1, -1):
        item = timeline.item(index)
        if item.isGroup:
            continue
        feature = adsk.fusion.ExtrudeFeature.cast(item.entity)
        if feature is None:
            continue
        if feature.operation == adsk.fusion.FeatureOperations.NewBodyFeatureOperation:
            return feature
        if fallback is None:
            fallback = feature
    if fallback is None:
        raise RuntimeError("no extrude in the timeline to change")
    return fallback


def run(_context: str):
    design = _design()
    feature = _extrude_to_edit(design)
    new = ARGS["thickness"] * MM
    if feature.extentType == adsk.fusion.FeatureExtentTypes.SymmetricFeatureExtentType:
        definition = feature.symmetricExtent
        if not definition.isFullLength:
            new = new / 2.0
    else:
        definition = adsk.fusion.DistanceExtentDefinition.cast(feature.extentOne)
    if definition is None:
        raise RuntimeError(feature.name + " has no distance to change (it is not a distance extrude)")
    parameter = definition.distance
    old = parameter.value
    parameter.value = -new if old < 0 else new
    _report("set_thickness", feature.name, edited=feature.name,
            was_mm=round(abs(old) / MM, 4), now_mm=round(abs(parameter.value) / MM, 4))
