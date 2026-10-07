# scale_body {factor}
# Uniform Scale feature on the last body, about the origin of the design: a
# body centred on the origin and standing on the XY plane stays there.
def run(_context: str):
    root = _root()
    body = _last_body(root)
    if ARGS["factor"] == 1:
        raise ValueError("a scale factor of 1 changes nothing")
    entities = adsk.core.ObjectCollection.create()
    entities.add(body)
    scales = root.features.scaleFeatures
    scale_input = scales.createInput(entities, root.originConstructionPoint, _vi(ARGS["factor"]))
    feature = scales.add(scale_input)
    _report("scale_body", feature.name, body=body.name, factor=ARGS["factor"])
