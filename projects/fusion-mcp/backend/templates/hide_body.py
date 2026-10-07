# hide_body {}
# Turns off the light bulb of the last body that is still visible. Nothing
# visible is an error (a call that changes nothing must not count as a step).
def run(_context: str):
    root = _root()
    for index in range(root.bRepBodies.count - 1, -1, -1):
        body = root.bRepBodies.item(index)
        if body.isLightBulbOn:
            body.isLightBulbOn = False
            _report("hide_body", None, hidden=[body.name])
            return
    raise RuntimeError("no visible body to hide")
