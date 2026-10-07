# chamfer {distance, edges}  (equal distance)
def run(_context: str):
    root = _root()
    edges = _edges(_last_body(root), ARGS["edges"])
    chamfers = root.features.chamferFeatures
    distance = _vi(ARGS["distance"] * MM)
    if hasattr(chamfers, "createInput2"):
        chamfer_input = chamfers.createInput2()
        chamfer_input.chamferEdgeSets.addEqualDistanceChamferEdgeSet(edges, distance, True)
    else:   # Fusion builds before createInput2 existed
        chamfer_input = chamfers.createInput(edges, True)
        chamfer_input.setToEqualDistance(distance)
    feature = chamfers.add(chamfer_input)
    _report("chamfer", feature.name, edges=edges.count)
