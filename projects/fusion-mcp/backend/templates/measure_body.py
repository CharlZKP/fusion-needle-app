# measure_body {}   (read-only)
# Bounding box in mm, volume in cm^3, area in cm^2, mass in kg (Fusion's
# internal units are cm and kg), for every solid body of the root component.
def run(_context: str):
    root = _root()
    measured = []
    for index in range(root.bRepBodies.count):
        body = root.bRepBodies.item(index)
        if not body.isSolid:
            continue
        box = body.boundingBox
        size = [round((high - low) / MM, 4)
                for low, high in zip(_xyz(box.minPoint), _xyz(box.maxPoint))]
        properties = body.physicalProperties
        material = body.material
        measured.append({"name": body.name, "size_mm": size,
                         "volume_cm3": round(body.volume, 6), "area_cm2": round(body.area, 6),
                         "mass_kg": round(properties.mass, 6),
                         "material": material.name if material is not None else None})
    if not measured:
        raise RuntimeError("no body in the design")
    _report("measure_body", None, measurements=measured)
