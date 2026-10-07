# split_body {direction, offset?}
# Splits the last body in two with a plane through the middle of its bounding
# box (plus offset along the plane's axis, if the call carried one):
#   horizontal - plane normal to the up axis: a top and a bottom half
#   vertical   - upright plane across the longest horizontal side
#   lengthwise - upright plane along the longest horizontal side
# The plane is an offset construction plane (hidden afterwards); it and the
# Split Body feature are one undo step.
_PLANE_OF_AXIS = {0: "yz", 1: "xz", 2: "xy"}


def run(_context: str):
    root = _root()
    body = _last_body(root)
    low, high = _box_span(body.boundingBox)
    axis = _split_axis(low, high, ARGS["direction"])
    middle = (low[axis] + high[axis]) / 2.0
    if "offset" in GIVEN:
        middle += ARGS["offset"] * MM
    if middle <= low[axis] + TOL or middle >= high[axis] - TOL:
        raise ValueError("the offset puts the cut outside the body")
    origin_plane = _origin_plane(root, _PLANE_OF_AXIS[axis])
    # setByOffset measures along the plane's own normal, which may point either way
    sign = 1.0 if _xyz(origin_plane.geometry.normal)[axis] > 0 else -1.0
    planes = root.constructionPlanes
    plane_input = planes.createInput()
    plane_input.setByOffset(origin_plane, _vi(middle * sign))
    plane = planes.add(plane_input)
    splits = root.features.splitBodyFeatures
    feature = splits.add(splits.createInput(body, plane, True))      # True: extend the splitting tool
    plane.isLightBulbOn = False
    _report("split_body", feature.name, axis="xyz"[axis], at_mm=round(middle / MM, 4))
