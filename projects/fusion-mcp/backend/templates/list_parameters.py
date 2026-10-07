# list_parameters {}   (read-only)
def run(_context: str):
    design = _design()
    parameters = design.userParameters
    listed = []
    for index in range(parameters.count):
        parameter = parameters.item(index)
        listed.append({"name": parameter.name, "expression": parameter.expression,
                       "unit": parameter.unit, "comment": parameter.comment})
    _report("list_parameters", None, parameters=listed)
