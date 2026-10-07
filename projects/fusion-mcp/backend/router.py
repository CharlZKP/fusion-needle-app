"""Choose the tool schemas of one turn.

    pick_tools("Ø6 hole at (10, 0)", "units: mm; sketch: none; bodies: Body1; last_feature: Extrude1")
    -> ["create_sketch", "extrude", "create_hole", "fillet", "chamfer"]

Needle sees at most 5 schemas per turn and the catalogue has more than 30, so
the app can no longer send a fixed five. ``pick_tools`` ranks the catalogue for
one request and returns exactly 5 names (fewer only if the catalogue is
smaller), always in catalogue order: the same set always gives the same list,
which keeps the server's agent cache warm.

Ranking, highest first:

1. tools a request word points at (``routing.keywords`` in tools/toolsets.yaml;
   weaker: word stems shared with the tool's own name, description, parameter
   names and enum values, and the enum phrases of tools/synonyms.yaml);
2. tools of a compound shape named in the request (``routing.compounds``:
   "plate 40x30x10" needs create_sketch, draw_rectangle and extrude; a pocket
   or boss takes its outline from the numbers, Ø = circle, otherwise
   rectangle), and create_sketch whenever a draw tool is wanted while no
   sketch is open;
3. tools that fit the state line (``routing.needs``);
4. padding by state (``routing.fill``), so a request with no known word still
   gets the tools most likely to be meant.

Tools listed under ``routing.keyword_only`` (the wrapper v3 tools) take part
only when one of their keywords is in the request. They are narrow one-step
tools next to a general one (``center_hole`` next to ``create_hole``); without
this rule their enum phrases ("on top") and parameter names ("depth") pushed
the general tools out of requests that never meant them.

It is a recall device, not a classifier: the model still decides, and still
answers ``[]`` when nothing fits.

Needs pyyaml.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent
TOOLS_DIR = BACKEND_DIR.parent / "tools"
MAX_TOOLS = 5

KEYWORD, COMPOUND, SYNONYM, STATE_FIT, STATE_MISFIT = 10.0, 8.0, 1.5, 1.0, -3.0
UNNAMED = -20.0                          # a routing.keyword_only tool that no keyword names
LEXICAL, LEXICAL_CAP = 3.0, 6.0          # per shared word stem (idf-weighted) with the tool's own text
STOPWORDS = frozenset("""a an the of in on at to for from by with and or as is it its this that be are into
each all any one two such not no optionally e.g mm degrees degree number numbers after before what which
named name request active most recent recently body bodies part model""".split())
DIMENSIONS = re.compile(r"\d\s*(?:x|×|by)\s*\d", re.IGNORECASE)
ROUND = re.compile(r"[Øø⌀]|\bdia(?:meter)?\b|\bround\b|\bcircular\b|\bD\s*=?\s*\d", re.IGNORECASE)
TRIPLE = re.compile(r"\d\s*(?:x|×|by)\s*\d+(?:\.\d+)?\s*(?:mm)?\s*(?:x|×|by)\s*\d", re.IGNORECASE)

_cache: dict | None = None


def _load() -> dict:
    global _cache
    if _cache is None:
        import yaml

        with open(TOOLS_DIR / "catalogue.json", encoding="utf-8") as handle:
            tools = json.load(handle)
        names = [tool["name"] for tool in tools]
        stems = {tool["name"]: _stems(_tool_text(tool)) for tool in tools}
        spread: dict = {}
        for own in stems.values():
            for stem in own:
                spread[stem] = spread.get(stem, 0) + 1
        # a stem shared by many tools says little: weight 1 for a unique stem, 0 at half the catalogue
        weight = {stem: max(0.0, 1.0 - (count - 1) / (len(names) / 2.0)) for stem, count in spread.items()}
        with open(TOOLS_DIR / "toolsets.yaml", encoding="utf-8") as handle:
            routing = (yaml.safe_load(handle) or {}).get("routing") or {}
        synonyms_path = TOOLS_DIR / "synonyms.yaml"
        synonyms = {}
        if synonyms_path.is_file():
            with open(synonyms_path, encoding="utf-8") as handle:
                synonyms = yaml.safe_load(handle) or {}
        weak: dict = {}
        for tool, params in synonyms.items():
            phrases = [str(p) for values in (params or {}).values() for listed in (values or {}).values()
                       for p in (listed if isinstance(listed, list) else [listed])]
            weak[tool] = [_compile(p) for p in phrases]
        _cache = {
            "names": names,
            "stems": stems,
            "stem_weight": weight,
            "order": {name: index for index, name in enumerate(names)},
            "keywords": {tool: [_compile(p) for p in phrases]
                         for tool, phrases in (routing.get("keywords") or {}).items()},
            "weak": weak,
            "needs": routing.get("needs") or {},
            "compounds": [([_compile(w) for w in entry["words"]], list(entry["tools"]))
                          for entry in routing.get("compounds") or []],
            "fill": routing.get("fill") or {},
            "keyword_only": list(routing.get("keyword_only") or []),
        }
        for tool in list(_cache["keywords"]) + list(_cache["needs"]) + _cache["keyword_only"] + \
                [t for _, tools in _cache["compounds"] for t in tools] + \
                [t for tools in _cache["fill"].values() for t in tools]:
            if tool not in _cache["order"]:
                raise ValueError(f"toolsets.yaml routing names an unknown tool {tool!r}")
    return _cache


def _stems(text: str) -> set[str]:
    """Lower-case word stems of a text: crude, only good enough to match plural and verb endings."""
    found = set()
    for word in re.findall(r"[a-z][a-z0-9]{2,}", text.lower().replace("_", " ")):
        if word in STOPWORDS:
            continue
        for ending in ("ing", "ed", "es", "s"):
            if word.endswith(ending) and len(word) - len(ending) >= 3:
                word = word[:-len(ending)]
                break
        found.add(word)
    return found


def _tool_text(tool: dict) -> str:
    props = tool.get("parameters", {}).get("properties", {})
    parts = [tool["name"], tool.get("description", "")]
    for key, spec in props.items():
        parts.append(key)
        parts.extend(str(value) for value in spec.get("enum", []))
    return " ".join(parts)


def _compile(phrase: str) -> re.Pattern:
    """Whole-word, case-insensitive, flexible inner whitespace; trailing * = any word ending."""
    phrase = str(phrase).strip()
    stem = phrase.endswith("*")
    if stem:
        phrase = phrase[:-1]
    head = r"(?<![A-Za-z0-9])" if phrase[:1].isascii() and phrase[:1].isalnum() else ""
    if stem:
        tail = r"[A-Za-z]*"
    else:
        tail = r"(?![A-Za-z0-9])" if phrase[-1:].isascii() and phrase[-1:].isalnum() else ""
    return re.compile(head + r"\s+".join(re.escape(part) for part in phrase.split()) + tail, re.IGNORECASE)


def parse_state_line(state_line: str) -> dict:
    """``{"sketch": bool, "bodies": int, "feature": bool}`` from the system facts; lenient."""
    facts = {}
    for part in (state_line or "").split(";"):
        key, _, value = part.partition(":")
        facts[key.strip().lower()] = value.strip()
    bodies = facts.get("bodies", "none")
    return {"sketch": facts.get("sketch", "none") not in ("", "none"),
            "bodies": 0 if bodies in ("", "none") else len([b for b in bodies.split(",") if b.strip()]),
            "feature": facts.get("last_feature", "none") not in ("", "none")}


def _need_met(need: str, state: dict) -> bool:
    return {"sketch": state["sketch"], "body": state["bodies"] >= 1, "bodies2": state["bodies"] >= 2,
            "feature": state["feature"]}.get(need, True)


def score_tools(query: str, state_line: str) -> dict[str, float]:
    """Score of every catalogue tool for this request and state (higher = more likely needed)."""
    data = _load()
    state = parse_state_line(state_line)
    text = query or ""
    scores = {name: 0.0 for name in data["names"]}
    named = set()
    for tool, patterns in data["keywords"].items():
        if any(pattern.search(text) for pattern in patterns):
            scores[tool] += KEYWORD
            named.add(tool)
    words = _stems(text)
    for tool, own in data["stems"].items():
        shared = words & own
        if shared:
            scores[tool] += min(LEXICAL_CAP, LEXICAL * sum(data["stem_weight"][stem] for stem in shared))
    for tool, patterns in data["weak"].items():
        if tool in scores and any(pattern.search(text) for pattern in patterns):
            scores[tool] += SYNONYM
    round_shape = ROUND.search(text) is not None
    opens_sketch = False
    for words, tools in data["compounds"]:
        if not any(pattern.search(text) for pattern in words):
            continue
        outline = [tool for tool in tools if data["needs"].get(tool) == "sketch" and tool != "extrude"]
        for tool in tools:
            if tool not in outline:
                scores[tool] += COMPOUND
        # the outline comes from the numbers when the word names none ("pocket 20x10",
        # "Ø13 boss"), and Ø / dia / D20 / round beats the word ("round bar" is no rectangle)
        if round_shape and outline in ([], ["draw_rectangle"]):
            outline = ["draw_circle"]
        elif not outline:
            # one size only ("5 mm boss, 10 high") can be either outline
            outline = ["draw_rectangle"] if DIMENSIONS.search(text) else ["draw_rectangle", "draw_circle"]
        opens_sketch = True
        for tool in outline:
            if tool in scores:
                scores[tool] += COMPOUND
    if TRIPLE.search(text) and not round_shape:                # 40x30x10: a block
        for tool in ("create_sketch", "draw_rectangle", "extrude"):
            if tool in scores:
                scores[tool] += COMPOUND
        opens_sketch = True
    elif DIMENSIONS.search(text) and not round_shape and "draw_rectangle" in scores:
        # "a 14 x 15 opening": two sizes are a rectangle, and it needs a sketch
        scores["draw_rectangle"] += COMPOUND if not state["sketch"] else SYNONYM
        opens_sketch = opens_sketch or not state["sketch"]
    draw_wanted = any(scores[tool] >= KEYWORD for tool in scores
                      if data["needs"].get(tool) == "sketch" and tool != "extrude")
    if draw_wanted and not state["sketch"] and "create_sketch" in scores:
        scores["create_sketch"] += COMPOUND                    # the answer has to open a sketch first
        opens_sketch = True
    for tool, need in data["needs"].items():
        met = _need_met(need, state) or (need == "sketch" and opens_sketch)
        scores[tool] += STATE_FIT if met else STATE_MISFIT
    # a keyword-only tool is offered when a routing word names it, never on word
    # overlap, an enum phrase ("on top") or a fitting state alone
    for tool in data["keyword_only"]:
        if tool not in named:
            scores[tool] = UNNAMED
    return scores


def route(query: str, state_line: str) -> dict:
    """``{"tools": [...], "matched": [...]}``: the pick, and which of them a keyword or compound hit.

    An empty ``matched`` means no routing word was found: the five are a guess from
    word overlap and state. The app may then prefer sending no ``tools`` at all
    (Needle's own retrieval over the whole catalogue) or asking the user.
    """
    scores = score_tools(query, state_line)
    tools = pick_tools(query, state_line)
    return {"tools": tools, "matched": [name for name in tools if scores[name] >= COMPOUND + STATE_MISFIT]}


def pick_tools(query: str, state_line: str) -> list[str]:
    """At most 5 tool names for this turn, in catalogue order."""
    data = _load()
    state = parse_state_line(state_line)
    scores = score_tools(query, state_line)
    fill_key = "sketch" if state["sketch"] else ("body" if state["bodies"] else "empty")
    fill = [tool for tool in data["fill"].get(fill_key, []) if tool in scores]
    fill_rank = {tool: index for index, tool in enumerate(fill)}
    ranked = sorted(data["names"],
                    key=lambda name: (-scores[name], fill_rank.get(name, len(fill)), data["order"][name]))
    chosen = ranked[:MAX_TOOLS]
    return sorted(chosen, key=data["order"].__getitem__)
