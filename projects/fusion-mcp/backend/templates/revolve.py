# revolve {axis, angle, operation}
# Revolves every profile of the active sketch around an origin axis. The axis
# must lie in the sketch plane (X or Y for a sketch on xy, X or Z on xz, Y or Z
# on yz); Fusion raises otherwise.
def run(_context: str):
    root = _root()
    sketch = _last_sketch(root)
    if sketch.profiles.count == 0:
        raise RuntimeError("sketch " + sketch.name + " has no closed profile to revolve")
    profiles = adsk.core.ObjectCollection.create()
    for index in range(sketch.profiles.count):
        profiles.add(sketch.profiles.item(index))
    operations = adsk.fusion.FeatureOperations
    operation = {"new_body": operations.NewBodyFeatureOperation,
                 "join": operations.JoinFeatureOperation,
                 "cut": operations.CutFeatureOperation,
                 "intersect": operations.IntersectFeatureOperation}[ARGS["operation"]]
    revolves = root.features.revolveFeatures
    revolve_input = revolves.createInput(profiles, _origin_axis(root, ARGS["axis"]), operation)
    revolve_input.setAngleExtent(False, _vi(math.radians(ARGS["angle"])))      # False: one side
    feature = revolves.add(revolve_input)
    _report("revolve", feature.name)
