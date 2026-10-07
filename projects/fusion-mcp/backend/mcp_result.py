"""Read what Autodesk's Fusion MCP server sends back.

Shapes, as recorded in third-party reference material for Fusion's MCP server (its client library and
the session log of calls made against a real Fusion):

* ``tools/call`` result: ``{"content": [{"type": "text", "text": "<JSON>"}]}``.
  The text of the first block is itself a JSON object.
* script (``fusion_mcp_execute`` / ``script``):
  ``{"message": "<everything the script printed>", "success": true}``.
* undo / redo: ``{"success": true, "message": "...", "canUndo": ..., "canRedo": ...}``,
  or ``{"success": false, "error": "Nothing to undo", ...}``.
* document queries: ``{"results": [{"name", "id", ...}], "success": true}``.
* activeCommand, idle: ``{"commandId": "SelectCommand", "commandName": "Select",
  "isDefaultCommand": true}``. Autodesk's tool description also names
  ``{"activeCommand": null}`` for "no dialog"; both are understood.
* screenshot: an MCP image block (``{"type": "image", "data": "<base64>",
  "mimeType": "image/png"}``) or, per the tool description, a JSON text with
  ``base64Data``. Which of the two a given build sends is not recorded; both
  are understood.

Not recorded anywhere in the reference: what a script that raised looks like
(``isError``, ``success: false`` or a JSON-RPC ``error``). All three are treated
as a failure here.

Every function accepts a JSON-RPC response, its ``result``, the ``content``
list, one content block, or plain text. Standard library only.
"""
from __future__ import annotations

import json


def _blocks(output) -> list:
    """Content blocks of a tool answer, in order."""
    if isinstance(output, str):
        return [{"type": "text", "text": output}]
    if isinstance(output, dict):
        if isinstance(output.get("result"), (dict, list)) and "content" not in output:
            return _blocks(output["result"])
        if "content" in output:
            return _blocks(output["content"])
        if output.get("type") in ("text", "image") or "text" in output:
            return [output]
        return [{"type": "text", "text": json.dumps(output)}]
    if isinstance(output, (list, tuple)):
        return [block for item in output for block in _blocks(item)]
    raise TypeError(f"cannot read a tool answer from {type(output).__name__}")


def texts(output) -> list[str]:
    """Text blocks of a tool answer."""
    return [str(block.get("text", "")) for block in _blocks(output) if block.get("type", "text") == "text"]


def _json_object(text: str):
    text = text.strip()
    if not text.startswith("{"):
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def payload(output) -> dict | None:
    """The JSON object the server put in its first text block, if there is one."""
    for text in texts(output):
        data = _json_object(text)
        if data is not None:
            return data
    return None


def printed(output) -> str:
    """What a script printed: the ``message`` of the server's JSON, else the raw text."""
    parts = []
    for text in texts(output):
        data = _json_object(text)
        if data is not None and isinstance(data.get("message"), str):
            parts.append(data["message"])
        elif data is not None and isinstance(data.get("output"), str):
            parts.append(data["output"])
        else:
            parts.append(text)
    return "\n".join(parts)


def report(output) -> dict | None:
    """The JSON line our own script printed (the last line that is a JSON object)."""
    for line in reversed(printed(output).splitlines()):
        data = _json_object(line)
        if data is not None:
            return data
    return None


def parse_result(output) -> dict:
    """``{"ok", "error", "message", "payload", "report"}`` for any tool answer.

    ``ok`` is False for a JSON-RPC error, ``isError``, or ``success: false``.
    ``report`` is the JSON line of a backend script (``{"tool", "feature", ...}``).
    """
    error = None
    if isinstance(output, dict) and isinstance(output.get("error"), (dict, str)) and "content" not in output \
            and "result" not in output:
        problem = output["error"]
        error = problem.get("message", json.dumps(problem)) if isinstance(problem, dict) else problem
        return {"ok": False, "error": str(error), "message": "", "payload": None, "report": None}
    result = output.get("result", output) if isinstance(output, dict) else output
    flagged = isinstance(result, dict) and result.get("isError") is True
    data = payload(output)
    message = printed(output)
    if data is not None and data.get("success") is False:
        error = str(data.get("error") or data.get("message") or "the tool reported success: false")
    elif flagged:
        error = message.strip() or "the tool reported an error"
    return {"ok": error is None, "error": error, "message": message, "payload": data,
            "report": report(output) if error is None else None}


def dialog_open(output) -> bool:
    """Answer of ``fusion_mcp_read {"queryType": "activeCommand"}``: is a command dialog open?

    Raises ValueError when the answer has neither known shape, so an unreadable
    answer is never taken for "no dialog".
    """
    data = payload(output)
    if data is None:
        raise ValueError("no JSON in the activeCommand answer")
    if "activeCommand" in data:
        command = data["activeCommand"]
        if command is None:
            return False
        if isinstance(command, dict):
            return command.get("isDefaultCommand") is not True
        raise ValueError("malformed activeCommand entry")
    if data.get("isDefaultCommand") is True:
        return False
    if "commandId" in data or "commandName" in data:
        return True
    raise ValueError("unknown activeCommand answer shape")


def search_results(output) -> list[dict]:
    """``[{"name", "id"}, ...]`` of a document query; entries without a string id are dropped."""
    data = payload(output)
    if data is None or not isinstance(data.get("results"), list):
        raise ValueError("no results list in the document answer")
    found = []
    for entry in data["results"]:
        if isinstance(entry, dict) and isinstance(entry.get("id"), str) and isinstance(entry.get("name"), str):
            found.append(entry)
    return found


def image(output) -> dict:
    """``{"mimeType", "data"}`` (base64) of a screenshot answer."""
    for block in _blocks(output):
        if block.get("type") == "image" and isinstance(block.get("data"), str):
            return {"mimeType": block.get("mimeType", "image/png"), "data": block["data"]}
    data = payload(output)
    if data is not None and isinstance(data.get("base64Data"), str):
        return {"mimeType": data.get("mimeType", "image/png"), "data": data["base64Data"]}
    raise ValueError("no image in the screenshot answer")
