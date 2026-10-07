# set_parameter {name, value, unit}
# Creates the user parameter, or changes its expression when it exists (the
# upsert of the reference's parametric examples). The expression is the number
# as written plus the unit; a parameter that exists keeps its own unit when the
# call names none.
_UNITS = {"mm": "mm", "cm": "cm", "inch": "in", "deg": "deg", "none": ""}


def _number_text(value):
    if type(value) is int:
        return str(value)
    text = "%.9f" % value
    return text.rstrip("0").rstrip(".") if "." in text else text


def run(_context: str):
    design = _design()
    parameters = design.userParameters
    parameter = parameters.itemByName(ARGS["name"])
    if parameter is not None and "unit" not in GIVEN:
        unit = parameter.unit
    else:
        unit = _UNITS[ARGS["unit"]]
    expression = _number_text(ARGS["value"]) + (" " + unit if unit else "")
    created = parameter is None
    if created:
        parameter = parameters.add(ARGS["name"], adsk.core.ValueInput.createByString(expression), unit, "")
    else:
        parameter.expression = expression
    _report("set_parameter", None, parameter=parameter.name, expression=parameter.expression,
            created=created)
