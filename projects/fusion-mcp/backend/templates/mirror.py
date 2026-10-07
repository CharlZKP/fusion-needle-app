# mirror {plane, target}
def run(_context: str):
    design = _design()
    root = design.rootComponent
    entities = _pattern_entities(design, root, ARGS["target"])
    mirrors = root.features.mirrorFeatures
    mirror_input = mirrors.createInput(entities, _origin_plane(root, ARGS["plane"]))
    feature = mirrors.add(mirror_input)
    _report("mirror", feature.name)
