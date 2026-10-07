# fillet {radius, edges}
def run(_context: str):
    root = _root()
    edges = _edges(_last_body(root), ARGS["edges"])
    fillets = root.features.filletFeatures
    fillet_input = fillets.createInput()
    fillet_input.addConstantRadiusEdgeSet(edges, _vi(ARGS["radius"] * MM), True)   # True: tangent chain
    feature = fillets.add(fillet_input)
    _report("fillet", feature.name, edges=edges.count)
