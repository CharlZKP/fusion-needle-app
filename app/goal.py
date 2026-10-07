"""Split a goal into ordered features (README.md)."""
from __future__ import annotations

import re

# →, ->, the whole word "then" (case-insensitive) and new lines
_SEPARATOR = re.compile(r"→|->|\r\n|\r|\n|\bthen\b", re.IGNORECASE)


def split_goal(goal: str) -> list[str]:
    """Features exactly as written: trimmed, empty pieces dropped, nothing reworded."""
    if not isinstance(goal, str):
        raise TypeError("goal must be a string")
    return [piece.strip() for piece in _SEPARATOR.split(goal) if piece.strip()]
