"""Words for the user: what to say, and what to say instead when a step was not run.

Both come from one data file, ``app/ui/examples.json`` (example requests keyed
by tool name), and from the project's catalogue and toolsets.yaml, so they
follow the catalogue: a tool the file does not know is shown with its
catalogue description, an entry for a tool that is gone is ignored.

    help_panel(...)  grouped example requests for the "What can I say?" panel
    refusal(...)     for an empty answer or a blocked one: what was understood,
                     what is missing or not supported, and 2-3 requests that work,
                     chosen from the tools that were offered for that request

Standard library only.
"""
from __future__ import annotations

import json
from pathlib import Path

from . import guard as guard_module
from .guard import NEED_WORDS, Request, Rules, need_met, param_family, parse_state_line

MAX_EXAMPLES = 3


class Examples:
    def __init__(self, data: dict | None = None, problem: str = ""):
        data = data if isinstance(data, dict) else {}
        self.problem = problem
        self.tools = {str(name): entry for name, entry in (data.get("tools") or {}).items()
                      if isinstance(entry, dict)} if isinstance(data.get("tools"), dict) else {}
        self.groups = data.get("groups") if isinstance(data.get("groups"), dict) else {}
        self.starters = [entry for entry in data.get("starters") or []
                         if isinstance(entry, dict) and isinstance(entry.get("text"), str)]

    @classmethod
    def load(cls, path) -> "Examples":
        try:
            with open(Path(path), encoding="utf-8") as handle:
                return cls(json.load(handle))
        except (OSError, ValueError) as failure:
            return cls({}, f"examples not read ({path}): {failure}")

    def of(self, name: str) -> list[str]:
        listed = (self.tools.get(name) or {}).get("examples")
        return [text for text in listed if isinstance(text, str) and text.strip()] if isinstance(listed, list) else []

    def label(self, name: str) -> str:
        label = (self.tools.get(name) or {}).get("label")
        return label if isinstance(label, str) and label.strip() else str(name).replace("_", " ")

    def ask(self, name: str, param: str) -> str:
        asks = (self.tools.get(name) or {}).get("ask")
        text = asks.get(param) if isinstance(asks, dict) else None
        return text if isinstance(text, str) and text.strip() else "the " + str(param).replace("_", " ")


def _by_name(catalogue: list) -> dict:
    return {tool["name"]: tool for tool in catalogue or [] if isinstance(tool, dict) and isinstance(tool.get("name"), str)}


def _join(parts: list[str]) -> str:
    parts = list(dict.fromkeys(part for part in parts if part))
    if len(parts) <= 1:
        return "".join(parts)
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def describe_call(call: dict) -> str:
    arguments = call.get("arguments") if isinstance(call.get("arguments"), dict) else {}
    inside = ", ".join(f"{key.replace('_', ' ')} {guard_module._show(value)}" for key, value in arguments.items())
    return f"{call.get('name')} ({inside})" if inside else str(call.get("name"))


def pick_examples(names: list[str], tools: dict, examples: Examples, state: dict | None = None,
                  rules: Rules | None = None, limit: int = MAX_EXAMPLES) -> list[dict]:
    """Up to ``limit`` requests that work, one tool after the other; tools that fit the state come first."""
    names = [name for name in dict.fromkeys(names) if name in tools]
    if state is not None and rules is not None:
        names.sort(key=lambda name: need_met(rules.needs.get(name, ""), state) is False)      # stable
    out: list[dict] = []
    for round_number in range(limit):
        for name in names:
            if len(out) >= limit:
                return out
            listed = examples.of(name)
            if round_number < len(listed):
                out.append({"tool": name, "text": listed[round_number]})
            elif round_number == 0:                          # no example on file: the catalogue's own words
                out.append({"tool": name, "description": str(tools[name].get("description") or name)})
    return out


def _missing(name: str, tool: dict, request: Request, state: dict, rules: Rules, examples: Examples) -> list[str]:
    """What a request for this tool lacks, in plain words. Empty when nothing can be named."""
    found: list[str] = []
    need = rules.needs.get(name)
    if need in NEED_WORDS and need_met(need, state) is False:
        wants, lacks, hint = NEED_WORDS[need]
        found.append(f"{examples.label(name).capitalize()} needs {wants}, and {lacks}." + (f" {hint}." if hint else ""))
    parameters = tool.get("parameters") if isinstance(tool.get("parameters"), dict) else {}
    properties = parameters.get("properties") if isinstance(parameters.get("properties"), dict) else {}
    wanted: list[str] = []
    numeric = 0
    for param in parameters.get("required") or []:
        spec = properties.get(param) if isinstance(properties.get(param), dict) else {}
        if "enum" in spec:
            named = any(request.has_phrase(rules.synonyms_for(name, param, value)) or request.names(str(value))
                        for value in spec["enum"])
            if not named:
                wanted.append(examples.ask(name, param))
        elif spec.get("type") in ("number", "integer"):
            numeric += 1
            family = param_family(param)
            if not any(guard_module._fits(place, family) for place in request.occurrences):
                wanted.append(examples.ask(name, param))
    written = len(guard_module._NUMBER.findall(request.raw)) + len(guard_module._word_occurrences(request.raw))
    if wanted:
        found.append(f"I need {_join(wanted)}.")
    elif numeric > written:
        asks = _join([examples.ask(name, param) for param in parameters.get("required") or []
                      if (properties.get(param) or {}).get("type") in ("number", "integer")])
        found.append(f"I need {numeric} numbers ({asks}) and the request writes {written}.")
    return found


def refusal(*, query: str, state_line: str, offered: list[str] | None, catalogue: list, rules: Rules,
            examples: Examples, calls: list | None = None, verdict: dict | None = None,
            engine_error: str = "") -> dict:
    """The message for a step that was not run.

    -> ``{"kind": "no_call" | "blocked", "understood": str, "problems": [str],
          "examples": [{"tool", "text"} | {"tool", "description"}], "text": str}``
    """
    tools = _by_name(catalogue)
    names = [name for name in (offered or list(tools)) if name in tools]
    state = parse_state_line(state_line)
    request = Request(query, rules.count_nouns)
    matched = rules.matched_tools(query, names)
    problems: list[str] = []
    called = [call.get("name") for call in calls or [] if isinstance(call, dict) and call.get("name") in tools]
    if calls:
        kind = "blocked"
        understood = "I read this as: " + ", then ".join(describe_call(call) for call in calls
                                                          if isinstance(call, dict)) + "."
        problems = [issue["message"] for issue in (verdict or {}).get("issues", [])]
        order = matched + called + names
    else:
        kind = "no_call"
        if matched:
            labels = [f"{examples.label(name)} ({name})" for name in matched[:2]]
            understood = "This reads like a request for " + " or ".join(labels) + "."
            for name in matched[:2]:
                problems = _missing(name, tools[name], request, state, rules, examples)
                if problems:
                    break
            if not problems:
                problems = ["The model found nothing it could call from this wording. Write each size as a number "
                            "and name the thing you want, as in the examples."]
            order = matched + names
        else:
            understood = "I could not match this to something I can do here."
            problems = ["It may not be supported yet, or it needs different words."]
            # nothing pointed at a tool: with the whole catalogue on offer, show what fits the design now
            order = names if offered else [name for name in names if need_met(rules.needs.get(name, ""), state)
                                           is not False]
        if engine_error:
            problems.append(f"The model gave up on this one ({engine_error}).")
    # the tools the request points at say most; the other tools on offer only fill up to two examples
    pointed = [name for name in order if name in matched or name in called]
    shown = pick_examples(pointed, tools, examples, state, rules) if pointed else []
    if len(shown) < 2:
        shown = pick_examples(order, tools, examples, state, rules)
    lines = [understood] + problems
    if shown:
        lines.append("Requests that work here: " + "; ".join(
            item.get("text") or f"{item['tool']}: {item['description']}" for item in shown))
    return {"kind": kind, "understood": understood, "problems": problems, "examples": shown,
            "text": " ".join(lines)}


def help_panel(catalogue: list, rules: Rules, examples: Examples) -> dict:
    """Grouped example requests for every catalogue tool, in the project's own grouping."""
    tools = _by_name(catalogue)
    groups = []
    seen: set[str] = set()

    def entry(name: str) -> dict:
        tool = tools[name]
        need = rules.needs.get(name)
        return {"name": name, "label": examples.label(name), "description": str(tool.get("description") or ""),
                "examples": examples.of(name), "needs": NEED_WORDS[need][0] if need in NEED_WORDS else ""}

    for group, members in rules.groups.items():
        members = [name for name in members if name in tools and name not in seen]
        if not members:
            continue
        seen.update(members)
        about = examples.groups.get(group) if isinstance(examples.groups.get(group), dict) else {}
        groups.append({"id": group, "title": str(about.get("title") or group.replace("_", " ").capitalize()),
                       "note": str(about.get("note") or ""), "tools": [entry(name) for name in members]})
    rest = [name for name in tools if name not in seen]
    if rest:
        groups.append({"id": "other", "title": "More" if groups else "What I can do", "note": "",
                       "tools": [entry(name) for name in rest]})
    starters = []
    for item in examples.starters:
        needed = item.get("tools") if isinstance(item.get("tools"), list) else []
        if all(name in tools for name in needed):
            starters.append({"text": item["text"], "note": str(item.get("note") or "")})
    return {"groups": groups, "starters": starters, "tools": len(tools),
            "problems": [text for text in [examples.problem] + list(rules.problems) if text]}
