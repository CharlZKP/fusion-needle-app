# combine {operation}
# Target = the first solid body of the root component, tools = all other solid
# bodies. join merges them into the target, cut subtracts them from it,
# intersect keeps what the target shares with them.
def run(_context: str):
    root = _root()
    solids = []
    for index in range(root.bRepBodies.count):
        body = root.bRepBodies.item(index)
        if body.isSolid:
            solids.append(body)
    if len(solids) < 2:
        raise RuntimeError("combine needs at least two bodies")
    tools = adsk.core.ObjectCollection.create()
    for body in solids[1:]:
        tools.add(body)
    operations = adsk.fusion.FeatureOperations
    combines = root.features.combineFeatures
    combine_input = combines.createInput(solids[0], tools)
    combine_input.operation = {"join": operations.JoinFeatureOperation,
                               "cut": operations.CutFeatureOperation,
                               "intersect": operations.IntersectFeatureOperation}[ARGS["operation"]]
    feature = combines.add(combine_input)
    _report("combine", feature.name)
