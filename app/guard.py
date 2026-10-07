"""Call guard: every model answer is checked here before anything is sent to Fusion.

The model is trained to copy argument values from the request ("labels are
literal", README.md). It mostly does. When it does not, the call is
held back and shown with the reason instead of being executed.

Three kinds of finding, all mechanical:

``schema``    unknown tool, tool not offered in this step, unknown / missing
              argument, wrong type, outside the bounds, unknown enum value.
              Never runnable.
``state``     the tool needs something the state line does not show
              (``routing.needs`` in the project's tools/toolsets.yaml) and no
              earlier call of the same answer can have created it. Not runnable
              as answered; the user fixes the request or the design.
``evidence``  a value is not backed by the request text. The user may still run
              the call ("Run anyway"); such a step is not kept as a history entry.

How evidence is decided (the number and enum rules are those of the training
data validator, src/stepserver/literal.py, re-stated here because the app ships
without stepserver):

* number   a number token of the request with the same value (``6``, ``6.0``,
           ``Ø6``, ``R2``, ``40x30x10``, ``20mm``, ``1,200``, ``2,5``, ``.5``) or
           a number word (``six``, ``twenty five``, ``a hundred``). A token is a
           maximal digit run: ``72`` never backs ``2``. A negative value needs a
           minus sign or the word minus / negative. "half", "double", "a few"
           are not numbers. Nothing is converted or computed.
* enum     a phrase listed for that value in tools/synonyms.yaml (whole words,
           case-insensitive); the list is authoritative when it exists. A value
           with no entry must be named in the request (``new_body`` also as
           ``new body``).
* name     (string with a schema ``pattern``) the exact characters, same case,
           as a whole token.
* string   any other string: a case-insensitive substring.

Two checks go beyond the validator, because the validator judges labels that
are already right while the guard judges a model that can be wrong:

* ``number_reused``  one value is used for more different arguments than the
           request writes it. The same argument in several calls (four holes of
           one diameter), ``x_`` / ``y_`` / ``z_`` twins (``x_spacing`` and
           ``y_spacing`` from one "pitch") and the sides of a "square" / "cube"
           count once.
* ``wrong_label``    every place the value is written carries a word that says
           it is something else: "4 holes" is a count, "5 mm margin" a margin,
           "10 mm from each corner" a position, "R2" a radius. Only arguments
           whose own name says what they are (diameter, depth, count, ...) are
           judged; ``value``, ``size`` and the like take any number.

An argument that equals its schema default is not judged: the call does the
same thing with or without it.

Spelling is folded on both sides before phrases are compared (aluminium /
aluminum, centre / center, millimetre / millimeter) and a unit glued to a
number is split off (``20mm`` -> ``20 mm``).

Standard library only; pyyaml is used for the two project files when it can be
imported, and every check that needs a file it cannot read is skipped.
"""
from __future__ import annotations

import json
import math
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path

SCHEMA, STATE, EVIDENCE = "schema", "state", "evidence"

# ---- text ------------------------------------------------------------------------

_SPELLING = (("aluminium", "aluminum"), ("centred", "centered"), ("centring", "centering"), ("centre", "center"),
             ("metre", "meter"), ("grey", "gray"), ("colour", "color"))
_GLUED_UNIT = re.compile(r"(\d)(mm|cm|inches|inch|degrees|degree|deg)(?![A-Za-z])", re.IGNORECASE)


def fold(text: str) -> str:
    """Spelling variants folded to one form and glued units split off. Used on both sides of a phrase match."""
    out = _GLUED_UNIT.sub(r"\1 \2", str(text or ""))
    for british, american in _SPELLING:
        out = re.sub(british, lambda found, word=american: _like(found.group(0), word), out, flags=re.IGNORECASE)
    return out


def _like(original: str, word: str) -> str:
    if original.isupper():
        return word.upper()
    return word.capitalize() if original[:1].isupper() else word


def phrase_in(phrase: str, text: str) -> bool:
    """Case-insensitive, flexible inner whitespace; a letter or digit end sits on a word boundary."""
    phrase = str(phrase).strip()
    if not phrase:
        return False
    head = r"(?<![A-Za-z0-9])" if phrase[0].isascii() and phrase[0].isalnum() else ""
    tail = r"(?![A-Za-z0-9])" if phrase[-1].isascii() and phrase[-1].isalnum() else ""
    pattern = head + r"\s+".join(re.escape(part) for part in phrase.split()) + tail
    return re.search(pattern, text, re.IGNORECASE) is not None


def keyword_pattern(phrase: str) -> re.Pattern:
    """A routing keyword of toolsets.yaml: like `phrase_in`, and a trailing * matches any word ending."""
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


# ---- numbers ---------------------------------------------------------------------

_NUMBER = re.compile(r"(?<![\d.])\d+(?:\.\d+)?")
_THOUSANDS = re.compile(r"(?<![\d.,])\d{1,3}(?:,\d{3})+(?:\.\d+)?(?![\d,])")
_DECIMAL_COMMA = re.compile(r"(?<![\d.,])\d+,\d+(?![\d,.])")
_LEADING_DOT = re.compile(r"(?<![\d])\.\d+")
_MINUS_BEFORE = re.compile(r"(?:[-−–]\s?|(?<![A-Za-z])(?:minus|negative)\s+)$", re.IGNORECASE)
_WORD = re.compile(r"[a-z]+")

_UNITS = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
          "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
          "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80,
         "ninety": 90}
_SCALES = {"hundred": 100, "thousand": 1000}


def _dec(value) -> Decimal | None:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number.normalize() if number.is_finite() else None


class Occurrence:
    """One place of the request where a number is written."""

    __slots__ = ("values", "start", "end", "negative", "families", "length_unit", "snippet")

    def __init__(self, values, start, end, negative):
        self.values = values
        self.start = start
        self.end = end
        self.negative = negative
        self.families: set[str] = set()         # what the words around it say it is
        self.length_unit = False
        self.snippet = ""


def _digit_occurrences(text: str) -> list[Occurrence]:
    """Every reading of every written number: ``10,5`` is 10.5, and also 10 and 5, each a place of its own."""
    found = []
    for pattern, clean in ((_NUMBER, lambda s: s), (_THOUSANDS, lambda s: s.replace(",", "")),
                           (_DECIMAL_COMMA, lambda s: s.replace(",", ".")), (_LEADING_DOT, lambda s: "0" + s)):
        for match in pattern.finditer(text):
            value = _dec(clean(match.group(0)))
            if value is not None:
                found.append(Occurrence({value}, match.start(), match.end(),
                                        _MINUS_BEFORE.search(text[:match.start()]) is not None))
    return found


def _word_occurrences(text: str) -> list[Occurrence]:
    """Number words and runs of them ('one hundred and twenty'), with the validator's arithmetic."""
    lowered = text.lower().replace("-", " ")
    words = [(match.group(0), match.start(), match.end()) for match in _WORD.finditer(lowered)]
    found: list[Occurrence] = []
    values: set[Decimal] = set()
    total = current = 0
    begin = finish = -1

    def flush():
        nonlocal values, total, current, begin, finish
        if begin >= 0:
            values.add(Decimal(total + current))
            found.append(Occurrence(values, begin, finish, _MINUS_BEFORE.search(lowered[:begin]) is not None))
        values, total, current, begin, finish = set(), 0, 0, -1, -1

    for index, (word, start, end) in enumerate(words):
        if word in _UNITS or word in _TENS:
            number = _UNITS.get(word, _TENS.get(word))
            values.add(Decimal(number))
            current += number
        elif word in _SCALES:
            current = (current or 1) * _SCALES[word]
            if _SCALES[word] >= 1000:
                total += current
                current = 0
        elif word == "and" and begin >= 0 and index + 1 < len(words) and (
                words[index + 1][0] in _UNITS or words[index + 1][0] in _TENS):
            continue
        else:
            flush()
            continue
        if begin < 0:
            begin = start
        finish = end
    flush()
    return found


# ---- what the words around a number say ---------------------------------------------

COUNT, ANGLE, ROUND, RADIUS, LINEAR, WIDTH, PLACE = "count", "angle", "diameter", "radius", "size", "width", "position"
FAMILY_WORDS = {COUNT: "a count", ANGLE: "an angle", ROUND: "a diameter", RADIUS: "a radius",
                LINEAR: "a depth, height or length", WIDTH: "a width", PLACE: "a position, margin or spacing"}
# what an argument of a family may be written as ("a hole 6 wide" is a diameter; "5 deep" is not)
_ACCEPTS = {COUNT: {COUNT}, ANGLE: {ANGLE}, ROUND: {ROUND, WIDTH}, RADIUS: {RADIUS}, LINEAR: {LINEAR, WIDTH},
            PLACE: {PLACE}}

# words of an argument name -> family; the first family in this order wins ("x_count" is a count)
_PARAM_WORDS = (
    (COUNT, {"count", "copies", "instances", "quantity", "number"}),
    (ANGLE, {"angle"}),
    (ROUND, {"diameter", "dia"}),
    (RADIUS, {"radius"}),
    (LINEAR, {"depth", "distance", "height", "thickness", "length", "width"}),
    (PLACE, {"margin", "inset", "offset", "spacing", "pitch", "x", "y", "z"}),
)
_AFTER_WORDS = {
    "deep": LINEAR, "depth": LINEAR, "thick": LINEAR, "thickness": LINEAR, "thk": LINEAR, "wide": WIDTH,
    "width": WIDTH, "tall": LINEAR, "high": LINEAR, "height": LINEAR, "long": LINEAR, "length": LINEAR,
    "apart": PLACE, "pitch": PLACE, "spacing": PLACE, "margin": PLACE, "inset": PLACE, "from": PLACE,
    "away": PLACE, "radius": RADIUS, "rad": RADIUS, "diameter": ROUND, "dia": ROUND,
    "times": COUNT, "copies": COUNT, "instances": COUNT, "pcs": COUNT, "pieces": COUNT,
    "rows": COUNT, "columns": COUNT, "places": COUNT, "bodies": COUNT,
}
_BEFORE_WORDS = {
    "diameter": ROUND, "dia": ROUND, "diam": ROUND, "radius": RADIUS, "rad": RADIUS, "depth": LINEAR,
    "width": WIDTH, "height": LINEAR, "length": LINEAR, "thickness": LINEAR, "margin": PLACE, "inset": PLACE,
    "spacing": PLACE, "pitch": PLACE, "offset": PLACE, "angle": ANGLE, "count": COUNT,
}
_UNIT_AFTER = re.compile(r"\s*(?:-\s*)?(?:(?P<length>mm|millimet(?:er|re)s?|cm|centimet(?:er|re)s?|inch(?:es)?|\")"
                         r"|(?P<angle>deg(?:rees?)?|°))(?![A-Za-z])", re.IGNORECASE)
_TIMES_AFTER = re.compile(r"\s*×(?!\s*[\d.])")
_WORDS_AFTER = re.compile(r"[\s\-]*([A-Za-z]+)(?:\s+([A-Za-z]+))?")
_NUMBER_NEXT = re.compile(r"\s*(?:(?:of|is|to)\s+)?[=:]?\s*[-−–]?\s*[Øø⌀]?\.?\d")
_NUMBER_BEHIND = re.compile(r"\d\s*(?:mm|cm|inch(?:es)?|\"|deg(?:rees?)?|°)?\s*$", re.IGNORECASE)   # "6 dia 1.5 DP"
_WORD_BEFORE = re.compile(r"(?<![A-Za-z])([A-Za-z]+)(?:\s+(?:of|is|to))?\s*[=:]?\s*$")
_GLUED_BEFORE = (
    (re.compile(r"[Øø⌀]\s*$"), ROUND),
    (re.compile(r"(?<![A-Za-z0-9])[Dd]$"), ROUND),
    (re.compile(r"(?<![A-Za-z0-9])[Rr]$"), RADIUS),
    (re.compile(r"(?:^|[^\d\s])\s*×\s*$"), COUNT),      # "pattern ×6"; "40 × 30" is a size
)


def param_family(name: str) -> str | None:
    words = set(re.split(r"[_\W]+", str(name).lower()))
    for family, known in _PARAM_WORDS:
        if words & known:
            return family
    return None


def _label(occurrence: Occurrence, text: str, count_nouns: set[str]):
    """Read the words next to a number: "R2", "dia 6", "5 deep", "4 holes", "5 mm margin", "×6", "90 degrees"."""
    before = text[:occurrence.start]
    start, cursor = occurrence.start, occurrence.end
    for pattern, family in _GLUED_BEFORE:
        found = pattern.search(before)
        if found:
            occurrence.families.add(family)
            start = min(start, len(before.rstrip()) - 1)
    word = _WORD_BEFORE.search(before)
    if word and word.group(1).lower() in _BEFORE_WORDS and not _NUMBER_BEHIND.search(before[:word.start(1)]):
        occurrence.families.add(_BEFORE_WORDS[word.group(1).lower()])
        start = word.start(1)
    unit = _UNIT_AFTER.match(text, cursor)
    if unit:
        cursor = unit.end()
        if unit.group("angle"):
            occurrence.families.add(ANGLE)
        else:
            occurrence.length_unit = True
    else:
        times = _TIMES_AFTER.match(text, cursor)
        if times:                                 # "4 × Ø6 holes": a multiplier; "40 × 30" is a size
            occurrence.families.add(COUNT)
            cursor = times.end()
    words = _WORDS_AFTER.match(text, cursor)
    if words:
        first, second = words.group(1).lower(), (words.group(2) or "").lower()
        family, reach = _AFTER_WORDS.get(first), words.end(1)
        if family is None and first == "in" and second == "from":
            family, reach = PLACE, words.end(2)   # "5 mm in from the edge"
        elif family is None and first in count_nouns:
            family = COUNT
        if family == COUNT and (unit or any(value != value.to_integral_value() for value in occurrence.values)):
            family = None                         # "6 mm holes" is a size, not six of them; nor is "0.25 corners"
        if family is not None and _NUMBER_NEXT.match(text, reach):
            family = None                         # "at 0, -5 depth 6", "R15 from 15° to 90°": it belongs to the next number
        if family is not None:
            occurrence.families.add(family)
            cursor = reach
    occurrence.snippet = " ".join(text[max(start, 0):cursor].split())


def _fits(occurrence: Occurrence, family: str | None) -> bool:
    """May an argument of this family take its value from this place of the request?"""
    if family is None:
        return True
    if occurrence.families:
        return bool(_ACCEPTS[family] & occurrence.families)
    if occurrence.length_unit:
        return family not in (COUNT, ANGLE)
    return True


class Request:
    """One request, parsed once: where numbers are written and what stands next to them."""

    def __init__(self, text: str, count_nouns: set[str] | None = None):
        self.raw = str(text or "")
        self.folded = fold(self.raw)
        self.occurrences = _digit_occurrences(self.raw) + _word_occurrences(self.raw)
        for occurrence in self.occurrences:
            _label(occurrence, self.raw, count_nouns or set())
        self.equal_sides = re.search(r"(?<![A-Za-z])(?:square|squared|squares|cube|cubes|cubic|cubical)(?![A-Za-z])",
                                     self.raw, re.IGNORECASE) is not None

    def places(self, value) -> list[Occurrence]:
        """Where a value is written. A negative value needs a minus; a positive one is backed by ``-5`` too."""
        number = _dec(value)
        if number is None:
            return []
        if number < 0:
            wanted = (-number).normalize()
            return [place for place in self.occurrences if place.negative and wanted in place.values]
        return [place for place in self.occurrences if number in place.values]

    def has_phrase(self, phrases) -> bool:
        return any(phrase_in(fold(phrase), self.folded) for phrase in phrases)

    def names(self, value: str) -> bool:
        value = str(value)
        variants = {value, value.replace("_", " "), value.replace("_", "-"), value.replace("_", "")}
        return any(phrase_in(fold(variant), self.folded) for variant in variants)

    def has_string(self, value: str) -> bool:
        return fold(str(value).strip()).casefold() in self.folded.casefold()

    def has_verbatim(self, value: str) -> bool:
        value = str(value)
        if not value or value != value.strip():
            return False
        pattern = (r"(?<![A-Za-z0-9_])(?<![A-Za-z0-9_][-.])" + re.escape(value)
                   + r"(?![A-Za-z0-9_])(?![-.][A-Za-z0-9_])")
        return re.search(pattern, self.raw) is not None


# ---- project rule files ------------------------------------------------------------

NEED_WORDS = {
    "sketch": ("an open sketch", "no sketch is open", "Start one first, for example: new sketch on xy"),
    "body": ("a body", "the design has no body yet", "Make the part first, for example: plate 40x30x10"),
    "bodies2": ("at least two bodies", "the design has fewer than two bodies", ""),
    "feature": ("an earlier feature to work on", "there is no feature yet", ""),
}


class Rules:
    """tools/synonyms.yaml, tools/toolsets.yaml and tools/catalogue.json of the project. Every piece is optional."""

    def __init__(self, synonyms: dict | None = None, toolsets: dict | None = None, catalogue: list | None = None,
                 problems: list[str] | None = None):
        self.synonyms = synonyms if isinstance(synonyms, dict) else None
        toolsets = toolsets if isinstance(toolsets, dict) else {}
        self.catalogue = [tool for tool in (catalogue or []) if isinstance(tool, dict) and isinstance(tool.get("name"), str)]
        self.problems = list(problems or [])
        routing = toolsets.get("routing") if isinstance(toolsets.get("routing"), dict) else {}
        needs = routing.get("needs") if isinstance(routing.get("needs"), dict) else {}
        self.needs = {str(tool): str(need) for tool, need in needs.items()}
        self.read_only = {str(name) for name in toolsets.get("read_only") or [] if isinstance(name, str)}
        self.groups: dict[str, list[str]] = {}
        self.group_of: dict[str, str] = {}
        groups = toolsets.get("groups") if isinstance(toolsets.get("groups"), dict) else {}
        for group, members in groups.items():
            members = [str(member) for member in (members or []) if isinstance(member, str)]
            self.groups[str(group)] = members
            for member in members:
                self.group_of.setdefault(member, str(group))
        self.keywords: dict[str, list[tuple[str, re.Pattern]]] = {}
        keywords = routing.get("keywords") if isinstance(routing.get("keywords"), dict) else {}
        for tool, phrases in keywords.items():
            if isinstance(phrases, str):
                phrases = [phrases]
            self.keywords[str(tool)] = [(str(phrase), keyword_pattern(phrase)) for phrase in phrases or []
                                        if str(phrase).strip("* ")]
        self.count_nouns = self._count_nouns()

    def _count_nouns(self) -> set[str]:
        """Plurals of the things the tools make: "4 holes", "3 slots" say how many, not how big."""
        singular: set[str] = set()
        for tool in list(self.keywords) + [entry["name"] for entry in self.catalogue] + list(self.needs):
            singular.update(part for part in tool.lower().split("_") if len(part) >= 3)
        for phrases in self.keywords.values():
            for phrase, _pattern in phrases:
                word = phrase.rstrip("*").strip().lower()
                if word.isalpha() and len(word) >= 3:
                    singular.add(word)
        nouns = set()
        for word in singular:
            if word.endswith("s"):
                continue
            nouns.add(word + "s")
            if word.endswith(("ch", "sh", "x")):
                nouns.add(word + "es")
        return nouns - set(_AFTER_WORDS) | {word for word, family in _AFTER_WORDS.items() if family == COUNT}

    @classmethod
    def load(cls, project_dir) -> "Rules":
        tools = Path(project_dir) / "tools"
        problems: list[str] = []
        catalogue = None
        try:
            with open(tools / "catalogue.json", encoding="utf-8") as handle:
                catalogue = json.load(handle)
            if not isinstance(catalogue, list):
                catalogue = None
        except (OSError, ValueError) as failure:
            problems.append(f"catalogue.json not read: {failure}")
        loaded = {}
        try:
            import yaml
        except Exception as failure:                    # frozen builds carry pyyaml; a bare Python may not
            yaml = None
            problems.append(f"pyyaml is missing ({failure}): enum words and state needs are not checked")
        for name in ("synonyms.yaml", "toolsets.yaml"):
            loaded[name] = None
            if yaml is None or not (tools / name).is_file():
                continue
            try:
                with open(tools / name, encoding="utf-8") as handle:
                    loaded[name] = yaml.safe_load(handle) or {}
            except Exception as failure:
                problems.append(f"{name} not read: {type(failure).__name__}: {failure}")
        return cls(loaded["synonyms.yaml"], loaded["toolsets.yaml"], catalogue, problems)

    def synonyms_for(self, tool: str, param: str, value) -> list[str]:
        if not self.synonyms:
            return []
        phrases: list[str] = []
        wanted = _value_key(value).casefold()
        for tool_key in (tool, "*"):
            params = self.synonyms.get(tool_key)
            values = params.get(param) if isinstance(params, dict) else None
            if not isinstance(values, dict):
                continue
            for key, listed in values.items():
                if _value_key(key).casefold() != wanted:
                    continue
                if isinstance(listed, str):
                    listed = [listed]
                phrases.extend(str(item) for item in listed or [])
        return phrases

    def matched_tools(self, query: str, names: list[str]) -> list[str]:
        """Those of ``names`` that a word of the request points at, the longest matching keyword first."""
        scored = []
        for position, name in enumerate(names):
            best = 0
            for phrase, pattern in self.keywords.get(name, []):
                if pattern.search(query or ""):
                    best = max(best, len(phrase))
            if best:
                scored.append((-best, position, name))
        return [name for _best, _position, name in sorted(scored)]


def _value_key(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        number = _dec(value)
        return format(number, "f") if number is not None else str(value)
    return str(value)


def parse_state_line(state_line: str) -> dict:
    """Facts of the state line; a fact the line does not hold is None (and is then not judged)."""
    facts = {}
    for part in (state_line or "").split(";"):
        key, colon, value = part.partition(":")
        if colon:
            facts[key.strip().lower()] = value.strip()
    out = {"sketch": None, "bodies": None, "feature": None}
    if "sketch" in facts:
        out["sketch"] = facts["sketch"] not in ("", "none")
    if "bodies" in facts:
        bodies = facts["bodies"]
        out["bodies"] = 0 if bodies in ("", "none") else len([name for name in bodies.split(",") if name.strip()])
    if "last_feature" in facts:
        out["feature"] = facts["last_feature"] not in ("", "none")
    return out


def need_met(need: str, state: dict) -> bool | None:
    """True / False, or None when the state line does not say."""
    if need == "sketch":
        return state["sketch"]
    if need == "body":
        return None if state["bodies"] is None else state["bodies"] >= 1
    if need == "bodies2":
        return None if state["bodies"] is None else state["bodies"] >= 2
    if need == "feature":
        return state["feature"]
    return None


# ---- the checks --------------------------------------------------------------------

def _issue(kind: str, code: str, position: int, tool: str, message: str, param: str = "", value=None) -> dict:
    out = {"kind": kind, "code": code, "call": position, "tool": tool, "message": message}
    if param:
        out["param"] = param
    if value is not None:
        out["value"] = value
    return out


def _show(value) -> str:
    if isinstance(value, float) and value.is_integer() and abs(value) < 1e15:
        return str(int(value))
    return str(value)


def _type_problem(spec: dict, value) -> str:
    kind = spec.get("type")
    if kind == "integer":
        return "" if type(value) is int else "must be a whole number"
    if kind == "number":
        if type(value) not in (int, float):
            return "must be a number"
        return "" if math.isfinite(value) else "must be a finite number"
    if kind == "string":
        return "" if type(value) is str else "must be text"
    if kind == "boolean":
        return "" if type(value) is bool else "must be true or false"
    return ""


def schema_issues(position: int, name, arguments, tool: dict | None, offered: list[str] | None) -> list[dict]:
    """What makes a call impossible to run, whatever the request said."""
    shown = name if isinstance(name, str) else repr(name)
    if not isinstance(name, str) or tool is None:
        return [_issue(SCHEMA, "unknown_tool", position, shown, f"{shown} is not a tool of this project.")]
    if offered is not None and name not in offered:
        return [_issue(SCHEMA, "not_offered", position, name,
                       f"{name} was not among the tools offered to the model for this request.")]
    if not isinstance(arguments, dict):
        return [_issue(SCHEMA, "bad_arguments", position, name, f"{name}: the arguments are not a list of named values.")]
    parameters = tool.get("parameters") if isinstance(tool.get("parameters"), dict) else {}
    properties = parameters.get("properties") if isinstance(parameters.get("properties"), dict) else {}
    issues = []
    for key in arguments:
        if key not in properties:
            issues.append(_issue(SCHEMA, "unknown_param", position, name, f"{name} has no argument called {key}.",
                                 str(key)))
    for key in parameters.get("required") or []:
        if key not in arguments:
            issues.append(_issue(SCHEMA, "missing_required", position, name,
                                 f"{name} needs {key.replace('_', ' ')}, and the call does not give it.", key))
    for key, value in arguments.items():
        spec = properties.get(key)
        if not isinstance(spec, dict):
            continue
        label = f"{name}: {key} = {_show(value)}"
        if "enum" in spec:
            if not isinstance(value, str) or value not in spec["enum"]:
                issues.append(_issue(SCHEMA, "bad_enum", position, name, f"{label} is not one of "
                                     + ", ".join(str(item) for item in spec["enum"]) + ".", key, value))
            continue
        problem = _type_problem(spec, value)
        if problem:
            issues.append(_issue(SCHEMA, "wrong_type", position, name, f"{label} {problem}.", key, value))
            continue
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            bound = ""
            if "minimum" in spec and value < spec["minimum"]:
                bound = f"must be at least {_show(spec['minimum'])}"
            elif "exclusiveMinimum" in spec and value <= spec["exclusiveMinimum"]:
                bound = f"must be greater than {_show(spec['exclusiveMinimum'])}"
            elif "maximum" in spec and value > spec["maximum"]:
                bound = f"must be at most {_show(spec['maximum'])}"
            elif "exclusiveMaximum" in spec and value >= spec["exclusiveMaximum"]:
                bound = f"must be less than {_show(spec['exclusiveMaximum'])}"
            if bound:
                issues.append(_issue(SCHEMA, "out_of_range", position, name, f"{label} {bound}.", key, value))
        elif isinstance(value, str):
            bad = ""
            if isinstance(spec.get("maxLength"), int) and len(value) > spec["maxLength"]:
                bad = f"is longer than {spec['maxLength']} characters"
            elif isinstance(spec.get("minLength"), int) and len(value) < spec["minLength"]:
                bad = f"is shorter than {spec['minLength']} characters"
            elif isinstance(spec.get("pattern"), str):
                try:
                    if re.fullmatch(spec["pattern"], value) is None:
                        bad = "has characters that are not allowed in a name"
                except re.error:
                    pass
            if bad:
                issues.append(_issue(SCHEMA, "bad_string", position, name, f"{label} {bad}.", key, value))
    return issues


def _same(default, value) -> bool:
    if isinstance(default, bool) or isinstance(value, bool) or isinstance(default, str) or isinstance(value, str):
        return type(default) is type(value) and default == value
    return default == value


def _role(tool: str, param: str) -> tuple[str, str]:
    """Arguments that take one written number together: x_spacing and y_spacing are one "spacing"."""
    if param in ("width", "height"):
        return tool, "width/height"               # "rect 20": one number for both sides
    return tool, re.sub(r"^[xyz]_", "", param)


class Guard:
    def __init__(self, rules: Rules | None = None):
        self.rules = rules or Rules()

    # -- evidence --
    def _evidence(self, request: Request, calls: list[dict], tools: dict, skip: set) -> list[dict]:
        issues: list[dict] = []
        numbers: list[tuple[int, str, str, object, str | None]] = []
        for position, call in enumerate(calls):
            name, arguments = call.get("name"), call.get("arguments") or {}
            if position in skip or not isinstance(arguments, dict):
                continue
            parameters = (tools.get(name) or {}).get("parameters") or {}
            properties = parameters.get("properties") or {}
            for param, value in arguments.items():
                spec = properties.get(param) if isinstance(properties.get(param), dict) else {}
                if value is None or isinstance(value, (dict, list)):
                    continue
                if "default" in spec and _same(spec["default"], value):
                    continue                              # same call with or without it
                where = f"{name}: {param} = {_show(value)}"
                if isinstance(value, str) and "pattern" in spec and "enum" not in spec and "const" not in spec:
                    if not request.has_verbatim(value):
                        issues.append(_issue(EVIDENCE, "name_not_verbatim", position, name,
                                             f"{where}, but the request does not write the name “{value}” "
                                             "exactly like that.", param, value))
                    continue
                phrases = self.rules.synonyms_for(name, param, value)
                if phrases and request.has_phrase(phrases):
                    continue
                if isinstance(value, bool):
                    if phrases:
                        issues.append(_issue(EVIDENCE, "not_named", position, name,
                                             f"{where}, but the request has none of the words that say so.",
                                             param, value))
                    continue
                if isinstance(value, (int, float)):
                    numbers.append((position, name, param, value, param_family(param)))
                    continue
                if not isinstance(value, str):
                    continue
                is_enum = "enum" in spec or "const" in spec
                if is_enum and self.rules.synonyms is None:
                    continue                              # no word list to judge by: left to the user
                if is_enum and phrases:
                    issues.append(_issue(EVIDENCE, "not_named", position, name,
                                         f"{where}, but the request does not say “{value.replace('_', ' ')}” "
                                         "(or a word that means it).", param, value))
                elif is_enum:
                    if not request.names(value):
                        issues.append(_issue(EVIDENCE, "not_named", position, name,
                                             f"{where}, but the request does not say “{value.replace('_', ' ')}”.",
                                             param, value))
                elif not request.has_string(value):
                    issues.append(_issue(EVIDENCE, "not_in_request", position, name,
                                         f"{where}, but “{value}” is not written in the request.", param, value))
        issues.extend(self._numbers(request, numbers))
        return issues

    def _numbers(self, request: Request, numbers: list) -> list[dict]:
        issues: list[dict] = []
        by_value: dict = {}
        for entry in numbers:
            position, name, param, value, family = entry
            places = request.places(value)
            where = f"{name}: {param} = {_show(value)}"
            if not places:
                issues.append(_issue(EVIDENCE, "not_in_request", position, name,
                                     f"{where}, but the number {_show(value)} is not written in the request.",
                                     param, value))
                continue
            if not any(_fits(place, family) for place in places):
                place = places[0]
                said = FAMILY_WORDS.get(sorted(place.families)[0], "something else") if place.families \
                    else "a measurement"
                issues.append(_issue(EVIDENCE, "wrong_label", position, name,
                                     f"{where}, but the request writes {_show(value)} as {said} "
                                     f"(“{place.snippet}”), not as the {param.replace('_', ' ')}.", param, value))
                continue
            by_value.setdefault(abs(_dec(value)), []).append(entry)
        for key, uses in by_value.items():
            roles: dict = {}
            for position, name, param, value, family in uses:
                role = ("*", LINEAR) if request.equal_sides and family == LINEAR else _role(name, param)
                roles.setdefault(role, (position, name, param, value))
            written = sum(1 for place in request.occurrences if key in place.values)    # "-5" also writes 5
            if len(roles) > written:
                used = sorted(roles.values())
                names = " and ".join(f"{param.replace('_', ' ')}" for _position, _name, param, _value in used)
                position, name, param, value = used[-1]
                times = "once" if written == 1 else f"{written} times"
                issues.append(_issue(EVIDENCE, "number_reused", position, name,
                                     f"The request writes {_show(abs(value))} {times}, but the answer uses it for "
                                     f"{len(roles)} different things: {names}.", param, value))
        return issues

    # -- state --
    def _state(self, state_line: str, calls: list[dict], skip: set) -> list[dict]:
        rules = self.rules
        if not rules.needs:
            return []
        state = parse_state_line(state_line)
        issues = []
        sketch_opened = body_made = False
        for position, call in enumerate(calls):
            name = call.get("name")
            if not isinstance(name, str):
                continue
            need = rules.needs.get(name)
            if need and position not in skip:
                met = need_met(need, state)
                # a later call may rest on what an earlier call of the same answer creates
                rescued = sketch_opened if need == "sketch" else body_made
                if met is False and not rescued and need in NEED_WORDS:
                    wants, lacks, hint = NEED_WORDS[need]
                    issues.append(_issue(STATE, "state_need", position, name,
                                         f"{name} needs {wants}, and {lacks}." + (f" {hint}." if hint else ""),
                                         value=need))
            group = rules.group_of.get(name, "")
            if need is None and (group == "sketch" or "sketch" in name):
                sketch_opened = True                      # create_sketch
            elif name not in rules.read_only and group != "sketch":
                body_made = True                          # extrude, a primitive, ...: may leave a body and a feature
        return issues

    # -- all of it --
    def check(self, query: str, state_line: str, calls: list, catalogue: list[dict],
              offered: list[str] | None = None, renderer_check=None, evidence: bool = True) -> dict:
        """Judge one answer.

        ``offered``         the tool names sent with the request (None: the whole catalogue)
        ``renderer_check``  ``(name, arguments) -> ''`` or the reason the project's renderer refuses the call
        ``evidence``        False for calls the user wrote by hand: only schema and state are judged
        -> ``{"decision": "pass" | "blocked", "overridable": bool, "issues": [...], "skipped": [...]}``
        """
        tools = {tool["name"]: tool for tool in catalogue if isinstance(tool, dict) and isinstance(tool.get("name"), str)}
        issues: list[dict] = []
        broken: set[int] = set()
        clean: list[dict] = []
        for position, call in enumerate(calls if isinstance(calls, list) else []):
            call = call if isinstance(call, dict) else {}
            name, arguments = call.get("name"), call.get("arguments")
            arguments = {} if arguments is None else arguments
            clean.append({"name": name, "arguments": arguments})
            found = schema_issues(position, name, arguments, tools.get(name) if isinstance(name, str) else None, offered)
            if not found and renderer_check is not None:
                problem = renderer_check(name, arguments)
                if problem:
                    found = [_issue(SCHEMA, "renderer", position, str(name), f"Cannot be run as answered: {problem}")]
            if any(item["code"] in ("unknown_tool", "not_offered", "bad_arguments") for item in found):
                broken.add(position)
            issues.extend(found)
        issues.extend(self._state(state_line, clean, broken))
        if evidence:
            request = Request(query, self.rules.count_nouns)
            issues.extend(self._evidence(request, clean, tools, broken))
        issues.sort(key=lambda item: (item["call"], (SCHEMA, STATE, EVIDENCE).index(item["kind"])))
        return {"decision": "blocked" if issues else "pass",
                "overridable": bool(issues) and all(item["kind"] == EVIDENCE for item in issues),
                "issues": issues, "skipped": list(self.rules.problems)}


# codes that mean "this value is not in the request" by the training rule itself (src/stepserver/literal.py);
# the other evidence codes are the guard's own, stricter reading
RULE_CODES = frozenset({"not_in_request", "not_named", "name_not_verbatim"})
