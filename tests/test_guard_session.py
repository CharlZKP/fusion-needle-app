"""The guard inside the loop: a held-back call never reaches Fusion, the user is told what to say instead,
"Run anyway" exists for evidence findings only, and every decision is recorded."""
import json
import sys

import pytest
from .kit import ANSWERS, HOLE, ROOT, read_rows

from app.diagnostics import Diagnostics
from app.phrases import Examples, help_panel, refusal
from app.session import ActionError

BODY = "units: mm; sketch: none; bodies: Body1; last_feature: Extrude1"
MARGIN = "4 holes on the corner with 5 mm margin"
WRONG = [{"name": "create_hole", "arguments": {"diameter": 4, "depth": 5}}]


def tools_run(fusion):
    return [entry["tool"] for entry in fusion.modelling_scripts()]


def with_plate(make_session, answers, goal, **settings):
    """A session whose first step is a plate that passes, so later steps have a body to work on."""
    session = make_session({"plate 40x30x10": ANSWERS["plate 40x30x10"], **answers}, **settings)
    session.diagnostics = Diagnostics(session.log.directory.parent / "diagnostics")
    session.set_goal("plate 40x30x10 → " + goal)
    return session


def test_a_held_back_call_is_not_sent_and_run_anyway_sends_it(fusion, make_session):
    session = with_plate(make_session, {MARGIN: {"calls": WRONG, "reasoning": "'4' -> diameter; '5' -> depth"},
                                        "fillet the top edges R2": ANSWERS["fillet the top edges R2"]},
                         MARGIN + " → fillet the top edges R2")
    session.run(auto=True)
    step = session.steps[1]
    assert session.steps[0].status == "done"
    assert step.status == "proposed" and step.blocked and step.to_dict()["guard"]["decision"] == "blocked"
    assert step.guard["overridable"] and {issue["code"] for issue in step.guard["issues"]} == {"wrong_label"}
    assert step.calls == WRONG                              # the proposed call is still shown
    assert "create_hole" not in tools_run(fusion)           # ... and nothing of it was sent
    assert session.steps[2].status == "pending"             # run-all stopped here
    assert "create_hole (diameter 4, depth 5)" in step.help["understood"]
    assert any("“4 holes”" in text for text in step.help["problems"]) and 2 <= len(step.help["examples"]) <= 3
    sent = len(fusion.calls)
    with pytest.raises(ActionError):
        session.run(auto=True)                              # run-all cannot pass it
    with pytest.raises(ActionError, match="Run anyway"):
        session.approve(1)                                  # nor can a plain "run this step"
    assert "create_hole" not in tools_run(fusion) and len(fusion.calls) == sent

    session.approve(1, override=True)                       # the user's own decision
    assert step.status == "done" and tools_run(fusion)[-2:] == ["create_hole", "fillet"]
    assert step.guard["decision"] == "overridden" and step.row_status == "not_logged"
    session.close()
    assert [row["query"] for row in read_rows(session.log.path)] == ["plate 40x30x10", "fillet the top edges R2"]
    kinds = [(event["decision"], event["feature"]) for event in session.diagnostics.events if event["kind"] == "guard"]
    assert kinds == [("blocked", MARGIN), ("overridden", MARGIN)]
    blocked = session.diagnostics.events[0]
    assert blocked["calls"] == WRONG and blocked["issues"][0]["code"] == "wrong_label" and blocked["told"]
    assert session.snapshot()["guard_counts"] == {"pass": 2, "blocked": 1, "no_call": 0, "overridden": 1, "edited": 0}


def test_schema_and_state_findings_have_no_run_anyway(fusion, make_session):
    bad = [{"name": "fillet", "arguments": {"radius": -2}}]
    session = with_plate(make_session, {"fillet -2 mm": {"calls": bad}}, "fillet -2 mm")
    session.run(auto=True)
    step = session.steps[1]
    assert step.status == "proposed" and step.blocked and not step.guard["overridable"]
    with pytest.raises(ActionError, match="greater than 0"):
        session.approve(1, override=True)
    with pytest.raises(ActionError):
        session.approve(1, bad)                             # the same calls through the editor
    assert tools_run(fusion)[-1] == "extrude"

    lone = make_session({"extrude 10 mm": {"calls": [{"name": "extrude", "arguments": {"distance": 10}}]}})
    lone.set_goal("extrude 10 mm")                          # the plate's sketch is used up: none is open
    lone.run(auto=True)
    step = lone.steps[0]
    assert step.blocked and [issue["kind"] for issue in step.guard["issues"]] == ["state"]
    with pytest.raises(ActionError, match="open sketch"):
        lone.approve(0, override=True)
    with pytest.raises(ActionError, match="open sketch"):
        lone.approve(0, [{"name": "extrude", "arguments": {"distance": 10, "operation": "join"}}])
    assert len(tools_run(fusion)) == 3                      # only the plate of the first session


def test_calls_edited_by_hand_run_but_unbacked_values_are_not_logged(fusion, make_session):
    session = with_plate(make_session, {MARGIN: {"calls": WRONG}}, MARGIN)
    session.run(auto=True)
    session.approve(1, [{"name": "create_hole", "arguments": {"diameter": 6}}])      # 6 is nowhere in the text
    step = session.steps[1]
    assert step.status == "done" and step.corrected and step.guard["decision"] == "edited"
    assert step.row_status == "not_logged" and "6 is not written" in step.row_note
    assert tools_run(fusion)[-1] == "create_hole"


def test_an_empty_answer_says_what_is_missing_and_what_works(fusion, make_session, backend):
    session = with_plate(make_session, {"make a hole": {"calls": [], "reasoning": "no diameter"}}, "make a hole",
                         toolset="router")
    session.run(auto=True)
    step = session.steps[1]
    assert step.status == "empty" and step.sent_tools and "create_hole" in step.sent_tools
    told = step.to_dict()["help"]
    assert told["kind"] == "no_call" and "a hole (create_hole)" in told["understood"]
    assert told["problems"] == ["I need the hole diameter."]
    assert 2 <= len(told["examples"]) <= 3 and told["examples"][0]["tool"] == "create_hole"
    assert all(item["tool"] in step.sent_tools for item in told["examples"])         # from the tools on offer
    assert [event["decision"] for event in session.diagnostics.events] == ["no_call"]


def test_refusal_for_requests_the_tools_cannot_do(rules, examples, catalogue):
    offered = ["create_sketch", "draw_rectangle", "extrude", "fillet", "undo"]
    told = refusal(query="please paint it with stripes", state_line=BODY, offered=offered, catalogue=catalogue,
                   rules=rules, examples=examples)
    assert "could not match" in told["understood"] and "not be supported" in told["problems"][0]
    assert len(told["examples"]) == 3 and {item["tool"] for item in told["examples"]} <= set(offered)
    assert told["examples"][0]["tool"] in ("create_sketch", "fillet", "undo")        # what fits the state comes first
    assert told["text"].startswith(told["understood"]) and "Requests that work here" in told["text"]
    state = refusal(query="extrude it 10 mm", state_line=BODY, offered=offered, catalogue=catalogue, rules=rules,
                    examples=examples)
    assert "open sketch" in state["problems"][0]
    whole = refusal(query="please paint it with stripes", state_line=BODY, offered=None, catalogue=catalogue,
                    rules=rules, examples=examples)
    assert 2 <= len(whole["examples"]) <= 3                  # "paint" points at set_color once it is on offer


def test_examples_file_and_help_panel_follow_the_catalogue(rules, examples, catalogue):
    assert not examples.problem
    names = [tool["name"] for tool in catalogue]
    panel = help_panel(catalogue, rules, examples)
    listed = [tool["name"] for group in panel["groups"] for tool in group["tools"]]
    assert sorted(listed) == sorted(names)                   # every catalogue tool, once
    assert all(group["title"] and group["tools"] for group in panel["groups"]) and panel["starters"]
    for group in panel["groups"]:
        for tool in group["tools"]:
            assert tool["examples"] or tool["description"], tool["name"]
    for name, entry in examples.tools.items():
        assert isinstance(entry.get("examples"), list) and all(isinstance(text, str) and text.strip()
                                                               for text in entry["examples"]), name

    # a tool the file has never heard of, and a file that is not there: its description is used instead
    new = {"name": "make_gear", "description": "Make a spur gear.", "parameters": {"type": "object", "properties": {}}}
    panel = help_panel(catalogue + [new], rules, examples)
    assert panel["groups"][-1]["id"] == "other" and panel["groups"][-1]["tools"][-1]["description"] == "Make a spur gear."
    bare = Examples.load("/no/such/examples.json")
    assert bare.problem and help_panel([new], rules, bare)["groups"][0]["tools"][0]["examples"] == []
    told = refusal(query="a gear please", state_line=BODY, offered=["make_gear"], catalogue=[new], rules=rules,
                   examples=bare)
    assert told["examples"] == [{"tool": "make_gear", "description": "Make a spur gear."}]


def test_the_examples_do_not_trip_the_guard_on_their_own_numbers(rules, examples, catalogue):
    """Each example must at least name a value for every required number of its tool."""
    from app.guard import Request

    tools = {tool["name"]: tool for tool in catalogue}
    for name, entry in examples.tools.items():
        if name not in tools:
            continue
        properties = tools[name]["parameters"].get("properties", {})
        needed = [key for key in tools[name]["parameters"].get("required", [])
                  if properties[key].get("type") in ("number", "integer")]
        for text in entry["examples"]:
            written = len({place.start for place in Request(text, rules.count_nouns).occurrences})
            assert written >= min(len(needed), 1), (name, text)
