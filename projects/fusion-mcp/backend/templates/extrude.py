# extrude {distance?, operation, direction, extent}
# Extrudes every profile of the active sketch.
# Direction when the request names none (direction not in GIVEN):
#   * cut / intersect through_all -> both sides of the sketch plane;
#   * cut / intersect by a distance, sketch on a body face -> into the body
#     (the sketch normal of a face sketch points out of the body);
#   * otherwise the schema default, positive.
def run(_context: str):
    root = _root()
    sketch = _last_sketch(root)
    if sketch.profiles.count == 0:
        raise RuntimeError("sketch " + sketch.name + " has no closed profile to extrude")
    profiles = adsk.core.ObjectCollection.create()
    for index in range(sketch.profiles.count):
        profiles.add(sketch.profiles.item(index))

    operations = adsk.fusion.FeatureOperations
    operation = {"new_body": operations.NewBodyFeatureOperation,
                 "join": operations.JoinFeatureOperation,
                 "cut": operations.CutFeatureOperation,
                 "intersect": operations.IntersectFeatureOperation}[ARGS["operation"]]
    removing = ARGS["operation"] in ("cut", "intersect")
    on_face = adsk.fusion.BRepFace.cast(sketch.referencePlane) is not None
    direction = ARGS["direction"]
    extent = ARGS["extent"]
    if "direction" not in GIVEN and removing:
        if extent == "through_all":
            direction = "symmetric"
        elif on_face:
            direction = "negative"

    extrudes = root.features.extrudeFeatures
    extrude_input = extrudes.createInput(profiles, operation)
    directions = adsk.fusion.ExtentDirections
    if extent == "through_all":
        extrude_input.setAllExtent({"positive": directions.PositiveExtentDirection,
                                    "negative": directions.NegativeExtentDirection,
                                    "symmetric": directions.SymmetricExtentDirection}[direction])
    else:
        if "distance" not in GIVEN:
            raise ValueError("extrude needs a distance unless extent is through_all")
        distance = ARGS["distance"] * MM
        if direction == "symmetric":
            extrude_input.setSymmetricExtent(_vi(distance), True)      # True: full length
        elif direction == "negative":
            extrude_input.setDistanceExtent(False, _vi(-distance))
        else:
            extrude_input.setDistanceExtent(False, _vi(distance))
    feature = extrudes.add(extrude_input)
    _report("extrude", feature.name, direction=direction)
