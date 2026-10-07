"""The shape of the calls a step returns."""

from __future__ import annotations


def fold_numbers(value):
    """6.0 -> 6 everywhere, nothing else touched (for output, not comparison)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, dict):
        return {key: fold_numbers(item) for key, item in value.items()}
    if isinstance(value, list):
        return [fold_numbers(item) for item in value]
    return value


def normalise_calls(calls) -> list[dict]:
    """Calls as [{"name", "arguments"}], tolerating None and missing arguments."""
    out = []
    for call in calls or []:
        if not isinstance(call, dict):
            continue
        arguments = call.get("arguments")
        out.append({"name": str(call.get("name")), "arguments": arguments if isinstance(arguments, dict) else {}})
    return out
