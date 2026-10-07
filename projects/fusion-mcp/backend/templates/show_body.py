# show_body {}
# Turns the light bulb of every hidden body back on. Nothing hidden is an
# error (a call that changes nothing must not count as a step).
def run(_context: str):
    root = _root()
    shown = []
    for index in range(root.bRepBodies.count):
        body = root.bRepBodies.item(index)
        if not body.isLightBulbOn:
            body.isLightBulbOn = True
            shown.append(body.name)
    if not shown:
        raise RuntimeError("no hidden body to show")
    _report("show_body", None, shown=shown)
