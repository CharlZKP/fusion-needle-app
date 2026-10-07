# bolt_hole {size, style, x, y}
# Through hole in the top face for a metric screw; x, y are model coordinates
# like create_hole on the top face. The sizes come from the tables below (mm),
# never from the request:
#   clearance   - ISO 273 medium series
#   counterbore - for a socket head cap screw (ISO 4762): diameter, depth
#   countersink - 90 degrees, for a countersunk screw (ISO 10642): outer diameter
_CLEARANCE = {"M3": 3.4, "M4": 4.5, "M5": 5.5, "M6": 6.6, "M8": 9.0, "M10": 11.0, "M12": 13.5}
_COUNTERBORE = {"M3": (6.5, 3.4), "M4": (8.0, 4.6), "M5": (10.0, 5.7), "M6": (11.0, 6.8),
                "M8": (15.0, 9.0), "M10": (18.0, 11.0), "M12": (20.0, 13.0)}
_COUNTERSINK = {"M3": 6.72, "M4": 8.96, "M5": 11.2, "M6": 13.44, "M8": 17.92, "M10": 22.4, "M12": 26.88}


def run(_context: str):
    root = _root()
    body = _last_body(root)
    face = _face(body, "top")
    span = _face_span(face, "top")
    position = _span_point(span, ARGS["x"] * MM, ARGS["y"] * MM)
    size, style = ARGS["size"], ARGS["style"]
    drill = _vi(_CLEARANCE[size] * MM)
    holes = root.features.holeFeatures
    if style == "counterbore":
        diameter, depth = _COUNTERBORE[size]
        hole_input = holes.createCounterboreInput(drill, _vi(diameter * MM), _vi(depth * MM))
    elif style == "countersink":
        hole_input = holes.createCountersinkInput(drill, _vi(_COUNTERSINK[size] * MM),
                                                  _vi(math.radians(90.0)))
    else:
        hole_input = holes.createSimpleInput(drill)
    hole_input.setPositionByPoint(face, position)
    hole_input.setAllExtent(adsk.fusion.ExtentDirections.PositiveExtentDirection)
    feature = holes.add(hole_input)
    _report("bolt_hole", feature.name, size=size, style=style, drill_mm=_CLEARANCE[size])
