# set_material {material}
# Assigns a physical material from Fusion's material libraries to the last body.
# Library and material names differ between Fusion versions and languages, so
# the lookup tries the exact names first and then any material whose name
# starts with the first candidate.
_NAMES = {"steel": ("Steel",), "stainless_steel": ("Stainless Steel",),
          "aluminum": ("Aluminum",), "brass": ("Brass",), "copper": ("Copper",),
          "titanium": ("Titanium",), "abs": ("ABS Plastic", "ABS"),
          "nylon": ("Nylon 6", "Nylon")}


def _find_material(app, names):
    libraries = app.materialLibraries
    for wanted in names:
        for index in range(libraries.count):
            material = libraries.item(index).materials.itemByName(wanted)
            if material is not None:
                return material
    prefix = names[0].lower()
    for index in range(libraries.count):
        materials = libraries.item(index).materials
        for number in range(materials.count):
            material = materials.item(number)
            if material.name.lower().startswith(prefix):
                return material
    raise RuntimeError("no material named " + names[0] + " in the material libraries")


def run(_context: str):
    root = _root()
    body = _last_body(root)
    material = _find_material(adsk.core.Application.get(), _NAMES[ARGS["material"]])
    body.material = material
    _report("set_material", None, body=body.name, material=material.name)
