# set_color {color}
# Appearance of the last body (looks only; set_material is the physical one).
# A design appearance "Needle <color>" is made once by copying a library
# appearance that has a "Color" property (a plastic or paint) and setting it.
_RGB = {"red": (200, 30, 30), "orange": (240, 130, 20), "yellow": (240, 210, 30),
        "green": (40, 160, 60), "blue": (30, 90, 200), "purple": (130, 60, 180),
        "pink": (240, 130, 170), "brown": (120, 75, 40), "black": (25, 25, 25),
        "white": (245, 245, 245), "gray": (128, 128, 128)}


def _color_property(appearance):
    return adsk.core.ColorProperty.cast(appearance.appearanceProperties.itemByName("Color"))


def _base_appearance(app):
    libraries = app.materialLibraries
    for index in range(libraries.count):
        appearances = libraries.item(index).appearances
        for number in range(appearances.count):
            appearance = appearances.item(number)
            name = appearance.name.lower()
            if ("plastic" in name or "paint" in name) and _color_property(appearance) is not None:
                return appearance
    raise RuntimeError("no library appearance with a settable colour was found")


def run(_context: str):
    design = _design()
    body = _last_body(design.rootComponent)
    name = "Needle " + ARGS["color"]
    appearance = design.appearances.itemByName(name)
    if appearance is None:
        appearance = design.appearances.addByCopy(_base_appearance(adsk.core.Application.get()), name)
        red, green, blue = _RGB[ARGS["color"]]
        _color_property(appearance).value = adsk.core.Color.create(red, green, blue, 255)
    body.appearance = appearance
    _report("set_color", None, body=body.name, appearance=appearance.name)
