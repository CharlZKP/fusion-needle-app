# create_sketch {plane, face?, offset?}
def run(_context: str):
    root = _root()
    if "face" in GIVEN:
        face = _face(_last_body(root), ARGS["face"])
        # addWithoutEdges: do not project the face outline, so the only
        # profiles are the ones drawn afterwards.
        sketch = root.sketches.addWithoutEdges(face)
        where = ARGS["face"] + " face"
    else:
        plane = _origin_plane(root, ARGS["plane"])
        where = ARGS["plane"]
        if "offset" in GIVEN and ARGS["offset"] != 0:
            planes = root.constructionPlanes
            plane_input = planes.createInput()
            plane_input.setByOffset(plane, _vi(ARGS["offset"] * MM))
            plane = planes.add(plane_input)
            where = "plane"
        sketch = root.sketches.add(plane)
    _report("create_sketch", sketch.name, sketch=sketch.name, on=where)
