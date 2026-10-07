# new_document {}
# A new, unsaved Fusion design document; it becomes the active document.
def run(_context: str):
    app = adsk.core.Application.get()
    document = app.documents.add(adsk.core.DocumentTypes.FusionDesignDocumentType)
    print(json.dumps({"tool": "new_document", "feature": None, "document": document.name}))
