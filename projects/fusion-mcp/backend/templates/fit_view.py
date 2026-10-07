# fit_view {}   (read-only: the camera is not part of the design)
def run(_context: str):
    viewport = adsk.core.Application.get().activeViewport
    viewport.fit()
    viewport.refresh()
    print(json.dumps({"tool": "fit_view", "feature": None}))
