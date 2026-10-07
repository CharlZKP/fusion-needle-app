"""Session history: one JSON line per step the user accepted.

Off by default (Settings, "Session history"). When it is on, entries go to
<per-user data folder>/history/<project>/<session>.jsonl, UTF-8, one object per
line, appended and never rewritten. The file stays on this machine.
"""
from __future__ import annotations

import datetime
import json
import os
import threading
from pathlib import Path

NO_CALL_REASONS = ("nocall_offtopic", "nocall_missing", "nocall_negated", "nocall_bounds")


def category_of(answers: list[dict]) -> str:
    if len(answers) == 1:
        return str(answers[0]["name"])
    return "multi"


def offered_schemas(sent: list[str] | None, answers: list[dict], catalogue: list[dict], limit: int) -> list[dict]:
    """Full schemas for the row's ``tools``.

    Names were sent: those schemas, in the order sent. ``tools`` was omitted
    (Needle retrieved): the called tools first, then other catalogue tools up to
    ``limit``, so the row holds every tool that ``answers`` calls.
    """
    by_name = {tool["name"]: tool for tool in catalogue}
    if sent:
        return [by_name[name] for name in sent if name in by_name]
    names: list[str] = []
    for call in answers:
        if call["name"] in by_name and call["name"] not in names:
            names.append(call["name"])
    for tool in catalogue:
        if len(names) >= limit:
            break
        if tool["name"] not in names:
            names.append(tool["name"])
    return [by_name[name] for name in names[:max(limit, 0)]]


class SessionLog:
    def __init__(self, directory: Path, model_name: str = "", now: datetime.datetime | None = None):
        self.directory = Path(directory)
        now = now or datetime.datetime.now()
        self.stamp = now.strftime("%Y%m%d-%H%M")
        session = now.strftime("%Y-%m-%d-%H%M")
        candidate, number = session, 1
        while (self.directory / f"{candidate}.jsonl").exists():
            number += 1
            candidate = f"{session}-{number}"
        self.session = candidate
        suffix = f"-s{number}" if number > 1 else ""
        self._part_base = f"session-{self.stamp}{suffix}"
        self.path = self.directory / f"{candidate}.jsonl"
        self.model_name = model_name
        self.rows_written = 0
        self._parts = 0
        self._lock = threading.Lock()

    def new_part(self) -> str:
        """meta.goal_id for one goal: every step of the goal lands in one split."""
        self._parts += 1
        return self._part_base if self._parts == 1 else f"{self._part_base}-g{self._parts}"

    def build_row(self, *, query: str, system: str, tools: list[dict], answers: list[dict], reasoning: str,
                  goal_id: str, number: int, category: str, status: str,
                  model_answers: list[dict] | None = None) -> dict:
        row = {"query": query, "system": system, "tools": tools, "answers": answers}
        if reasoning:
            row["reasoning"] = reasoning
        meta = {"id": f"{goal_id}-{number}", "goal_id": goal_id, "category": category, "source": "app",
                "status": status, "model": self.model_name,
                "executed_ok": True, "corrected": status == "corrected"}
        if model_answers is not None:
            meta["model_answers"] = model_answers
        row["meta"] = meta
        return row

    def append(self, row: dict) -> int:
        """Append one row; returns its line number in the file."""
        line = json.dumps(row, ensure_ascii=False, separators=(", ", ": "))
        with self._lock:
            self.directory.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8", newline="\n") as handle:
                handle.write(line + "\n")
                handle.flush()
                try:
                    os.fsync(handle.fileno())
                except OSError:
                    pass
            self.rows_written += 1
            return self.rows_written


