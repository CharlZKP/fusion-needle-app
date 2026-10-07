# delete_body {}
# Removes the last body. In a parametric design that is a Remove feature in the
# timeline (the body can come back with undo or by deleting the feature); in a
# direct-modelling design the body is deleted.
def run(_context: str):
    design = _design()
    root = design.rootComponent
    body = _last_body(root)
    name = body.name
    if design.designType == adsk.fusion.DesignTypes.ParametricDesignType:
        feature = root.features.removeFeatures.add(body)
        _report("delete_body", feature.name, deleted=name)
    else:
        body.deleteMe()
        _report("delete_body", None, deleted=name)
