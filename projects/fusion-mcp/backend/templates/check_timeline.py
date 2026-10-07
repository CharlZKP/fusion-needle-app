# check_timeline {}   (read-only)
# Timeline items whose health is not "healthy", with Fusion's message.
def run(_context: str):
    design = _design()
    timeline = design.timeline
    unhealthy = []
    for index in range(timeline.count):
        item = timeline.item(index)
        if item.healthState != adsk.fusion.FeatureHealthStates.HealthyFeatureHealthState:
            unhealthy.append({"index": index, "name": item.name, "message": item.errorOrWarningMessage})
    _report("check_timeline", None, items=timeline.count, healthy=not unhealthy, unhealthy=unhealthy)
