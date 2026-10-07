# shell {thickness, open_face}
def run(_context: str):
    root = _root()
    body = _last_body(root)
    removed = adsk.core.ObjectCollection.create()
    removed.add(_face(body, ARGS["open_face"]))
    shells = root.features.shellFeatures
    shell_input = shells.createInput(removed, False)      # False: no tangent chain
    shell_input.insideThickness = _vi(ARGS["thickness"] * MM)
    feature = shells.add(shell_input)
    _report("shell", feature.name)
