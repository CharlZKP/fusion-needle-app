"""The loop of README.md against the fake Fusion and a scripted model."""
import json
import sys

import pytest
from .kit import ANSWERS, FILLET, GOAL, HOLE, PLATE, ROOT, read_rows

from app.backend import Backend
from app.session import ActionError

EMPTY_STATE = "units: mm; sketch: none; bodies: none; last_feature: none"


def tools_run(fusion):
    return [(entry["tool"], entry["args"]) for entry in fusion.modelling_scripts()]


def test_run_all_executes_every_feature_in_order(fusion, make_session):
    session = make_session()
    session.set_goal(GOAL)
    assert [step.text for step in session.steps] == list(ANSWERS)
    session.run(auto=True)
    assert [step.status for step in session.steps] == ["done"] * 4
    assert [name for name, _ in tools_run(fusion)] == [
        "create_sketch", "draw_rectangle", "extrude", "create_hole", "circular_pattern", "fillet"]
    # schema defaults are filled by the renderer, the given values are the model's
    assert dict(tools_run(fusion))["draw_rectangle"]["width"] == 40
    assert dict(tools_run(fusion))["fillet"] == {"radius": 2, "edges": "top"}
    # the model saw one feature and the state read just before it
    sent = session.fake_model.steps
    assert [body["query"] for body in sent] == list(ANSWERS)
    assert sent[0]["system"] == EMPTY_STATE
    assert sent[1]["system"] == "units: mm; sketch: none; bodies: Body1; last_feature: Extrude1"
    assert sent[3]["system"].endswith("last_feature: CircularPattern1")
    assert session.steps[3].state_after.endswith("last_feature: Fillet1")
    assert fusion.undos == 0
    assert session.snapshot()["finished"] is True


def test_rows_have_the_contract_format(fusion, make_session):
    session = make_session()
    session.set_goal(GOAL)
    session.run(auto=True)
    assert session.log.rows_written == 3 and session.snapshot()["log"]["pending"] == 1
    session.close()                                         # moving on writes the last row
    rows = read_rows(session.log.path)
    assert len(rows) == 4
    first = rows[0]
    assert list(first) == ["query", "system", "tools", "answers", "reasoning", "meta"]
    assert first["query"] == "plate 40x30x10" and first["system"] == EMPTY_STATE
    assert first["answers"] == PLATE
    assert first["reasoning"] == ANSWERS["plate 40x30x10"]["reasoning"]
    assert all(set(tool) >= {"name", "description", "parameters"} for tool in first["tools"])
    assert 1 <= len(first["tools"]) <= 5
    assert {call["name"] for call in PLATE} <= {tool["name"] for tool in first["tools"]}
    meta = first["meta"]
    assert meta["source"] == "app" and meta["status"] == "executed" and meta["category"] == "multi"
    assert meta["model"] == "fake-20L.cact" and meta["id"] == meta["goal_id"] + "-1"
    assert [row["meta"]["category"] for row in rows] == ["multi", "create_hole", "circular_pattern", "fillet"]
    assert len({row["meta"]["goal_id"] for row in rows}) == 1
    assert len({row["meta"]["id"] for row in rows}) == 4
    assert session.log.path.parent.name == "history" and session.log.path.suffix == ".jsonl"
    assert open(session.log.path, "rb").read().count(b"\n") == 4


def test_failure_undoes_once_per_done_call_and_logs_nothing(fusion, make_session):
    fusion.fail_tools["extrude"] = "Traceback (most recent call last):\nRuntimeError: profile is open"
    session = make_session()
    session.set_goal(GOAL)
    session.run(auto=True)
    step = session.steps[0]
    assert step.status == "failed" and step.error_call == 2 and "profile is open" in step.error
    assert fusion.undos == 2 and step.undone == 2           # create_sketch and draw_rectangle were in
    assert fusion.design["sketch"] is None and fusion.design["bodies"] == []
    assert [s.status for s in session.steps[1:]] == ["pending"] * 3     # the loop stopped
    assert len(session.fake_model.steps) == 1
    assert session.fusion_state == EMPTY_STATE              # state re-read after the rollback
    session.close()
    assert read_rows(session.log.path) == []
    # the user fixes the cause and tries again: the step is asked afresh
    del fusion.fail_tools["extrude"]
    session.retry(0)
    assert [step.status for step in session.steps] == ["done"] * 4 and fusion.undos == 2    # still run-all


@pytest.mark.parametrize("style", ["isError", "success_false", "jsonrpc"])
def test_every_failure_shape_stops_the_loop(fusion, make_session, style):
    fusion.fail_style = style
    fusion.fail_tools["draw_rectangle"] = "boom"
    session = make_session()
    session.set_goal("plate 40x30x10")
    session.run(auto=True)
    assert session.steps[0].status == "failed" and session.steps[0].error_call == 1
    assert fusion.undos == 1


def test_first_call_failing_sends_no_undo(fusion, make_session):
    fusion.fail_tools["create_sketch"] = "boom"
    session = make_session()
    session.set_goal("plate 40x30x10")
    session.run(auto=True)
    assert session.steps[0].status == "failed" and fusion.undos == 0


def test_read_only_and_no_undo_calls_are_not_counted(fusion, make_session, backend, monkeypatch):
    flags = {"create_sketch": {"confirm": False, "read_only": True},
             "draw_rectangle": {"confirm": False, "read_only": False, "undoable": False}}
    monkeypatch.setattr(backend, "call_info", lambda name: {
        "confirm": False, "read_only": False, "undoable": None, **flags.get(name, {})})
    fusion.fail_tools["extrude"] = "boom"
    session = make_session()
    session.set_goal("plate 40x30x10")
    reads_before = sum(1 for call in fusion.calls if call["arguments"].get("queryType") == "activeCommand")
    session.run(auto=True)
    assert session.steps[0].status == "failed" and fusion.undos == 0
    dialog_checks = sum(1 for call in fusion.calls if call["arguments"].get("queryType") == "activeCommand")
    assert dialog_checks - reads_before == 2                # none before the read-only call


def test_the_models_own_undo_is_not_rolled_back(fusion, make_session):
    answers = {"plate 40x30x10": ANSWERS["plate 40x30x10"],
               "undo and fillet R2": {"calls": [{"name": "undo", "arguments": {}},
                                                {"name": "fillet", "arguments": {"radius": 2}}]}}
    session = make_session(answers)
    session.set_goal("plate 40x30x10 → undo and fillet R2 then zoom to fit")
    assert len(session.steps) == 3                          # "then" splits; use the pieces as written
    session.run(auto=True)
    # undo took the extrude back, so the fillet has no body and fails; the undo is not "undone" again
    assert session.steps[1].status == "failed" and fusion.undos == 1


def test_confirm_gating_suppressed_calls_are_never_executed(fusion, make_session):
    answers = {"Ø6 hole at (10, 0)": {"calls": HOLE, "suppressed": [{"name": "fillet", "arguments": {"radius": 9}}]}}
    session = make_session(answers)
    session.set_goal("Ø6 hole at (10, 0)")
    fusion.design["bodies"] = ["Body1"]
    session.run(auto=True)
    step = session.steps[0]
    assert step.status == "proposed" and "withheld" in step.confirm[0]
    assert tools_run(fusion) == []                          # nothing ran without the user's yes
    with pytest.raises(ActionError):
        session.run(auto=True)                              # run-all cannot skip the question
    fusion.fail_tools["create_hole"] = "the design has no body"
    session.approve(0)
    assert [name for name, _ in tools_run(fusion)] == ["create_hole"]   # fillet (suppressed) never sent
    assert step.status == "failed"


def test_confirm_gating_too_many_calls_and_flagged_tools(fusion, make_session, backend, monkeypatch):
    session = make_session(confirm_over_calls=2)
    session.set_goal("plate 40x30x10")
    session.run(auto=True)
    assert session.steps[0].status == "proposed" and "3 calls" in session.steps[0].confirm[0]
    session.approve(0)
    assert session.steps[0].status == "done"

    monkeypatch.setattr(backend, "call_info", lambda name: {"confirm": name == "create_hole", "read_only": False,
                                                            "undoable": None})
    session.set_goal("Ø6 hole at (10, 0)")
    session.run(auto=True)
    assert session.steps[0].status == "proposed"
    assert session.steps[0].confirm == ["create_hole needs your confirmation"]
    session.approve(0)
    assert session.steps[0].status == "done"


def test_step_mode_waits_for_approval_each_time(fusion, make_session):
    session = make_session()
    session.set_goal(GOAL)
    session.run(auto=False)
    assert [step.status for step in session.steps] == ["proposed", "pending", "pending", "pending"]
    assert tools_run(fusion) == []
    session.approve(0)
    assert [step.status for step in session.steps] == ["done", "proposed", "pending", "pending"]
    with pytest.raises(ActionError):
        session.approve(2)                                  # not out of order
    session.run(auto=True)                                  # switch to run-all for the rest
    assert [step.status for step in session.steps] == ["done"] * 4


def test_empty_answer_stops_and_continues_from_the_same_feature(fusion, make_session):
    answers = dict(ANSWERS)
    answers["make a hole"] = {"calls": [], "reasoning": "no diameter given",
                              "suppressed": [{"name": "create_hole", "arguments": {"diameter": 5}}]}
    session = make_session(answers)
    session.set_goal("plate 40x30x10 → make a hole → fillet the top edges R2")
    session.run(auto=True)
    assert [step.status for step in session.steps] == ["done", "empty", "pending"]
    shown = session.steps[1].to_dict()
    assert shown["reasoning"] == "no diameter given" and shown["suppressed"][0]["name"] == "create_hole"
    assert [name for name, _ in tools_run(fusion)] == ["create_sketch", "draw_rectangle", "extrude"]  # kept
    asked = len(session.fake_model.steps)
    with pytest.raises(ActionError):
        session.rewrite(1, "make a hole")                   # the same text is not retried
    with pytest.raises(ActionError):
        session.run(auto=True)
    assert len(session.fake_model.steps) == asked
    session.rewrite(1, "Ø6 hole at (10, 0)")                # continues from this feature, in run-all mode
    assert [step.status for step in session.steps] == ["done", "done", "done"]
    session.close()
    rows = read_rows(session.log.path)
    assert [row["query"] for row in rows] == ["plate 40x30x10", "Ø6 hole at (10, 0)", "fillet the top edges R2"]


def test_engine_error_is_treated_like_an_empty_answer(fusion, make_session):
    answers = {"plate": {"calls": [], "engine_error": "tool call truncated: token budget exhausted"}}
    session = make_session(answers)
    session.set_goal("plate")
    session.run(auto=True)
    assert session.steps[0].status == "empty" and "truncated" in session.steps[0].to_dict()["engine_error"]


def test_agreed_empty_answer_is_logged_as_a_negative(fusion, make_session):
    answers = {"what is the weather": {"calls": [], "reasoning": "not a modelling request"},
               "plate 40x30x10": ANSWERS["plate 40x30x10"]}
    session = make_session(answers)
    session.set_goal("what is the weather → plate 40x30x10")
    session.run(auto=True)
    with pytest.raises(ActionError):
        session.accept_empty(0, "multi")
    session.accept_empty(0, "nocall_offtopic")
    assert [step.status for step in session.steps] == ["negative", "done"]
    session.close()
    rows = read_rows(session.log.path)
    assert rows[0]["answers"] == [] and rows[0]["meta"]["category"] == "nocall_offtopic"
    assert 1 <= len(rows[0]["tools"]) <= 5 and rows[1]["meta"]["category"] == "multi"


def test_skipped_step_is_not_logged(fusion, make_session):
    answers = {"make a hole": {"calls": []}, "plate 40x30x10": ANSWERS["plate 40x30x10"]}
    session = make_session(answers)
    session.set_goal("make a hole → plate 40x30x10")
    session.run(auto=True)
    session.skip(0)
    assert [step.status for step in session.steps] == ["skipped", "done"]
    session.close()
    assert [row["query"] for row in read_rows(session.log.path)] == ["plate 40x30x10"]


def test_corrected_calls_are_logged_as_corrected(fusion, make_session):
    wrong = [{"name": "create_hole", "arguments": {"diameter": 10, "x": 6}}]
    session = make_session({"Ø6 hole at (10, 0)": {"calls": wrong, "reasoning": "mixed up"},
                            "plate 40x30x10": ANSWERS["plate 40x30x10"]})
    session.set_goal("plate 40x30x10 → Ø6 hole at (10, 0)")
    session.run(auto=False)
    session.approve(0)                                      # unchanged: executed
    with pytest.raises(ActionError):                        # schema is enforced on edits
        session.approve(1, [{"name": "create_hole", "arguments": {"diameter": -6}}])
    with pytest.raises(ActionError):
        session.approve(1, [{"name": "create_hole", "arguments": {"diameter": 6, "bogus": 1}}])
    assert tools_run(fusion)[-1][0] == "extrude"            # the bad edits ran nothing
    session.approve(1, HOLE)
    assert session.steps[1].status == "done" and session.steps[1].corrected
    assert tools_run(fusion)[-1] == ("create_hole", {**tools_run(fusion)[-1][1], "diameter": 6, "x": 10, "y": 0})
    session.close()
    rows = read_rows(session.log.path)
    assert rows[0]["meta"]["status"] == "executed" and rows[0]["meta"]["corrected"] is False
    corrected = rows[1]
    assert corrected["answers"] == HOLE and corrected["meta"]["status"] == "corrected"
    assert corrected["meta"]["corrected"] is True and corrected["meta"]["model_answers"] == wrong
    assert "reasoning" not in corrected                     # the model's reasoning explained the wrong calls


def test_edit_cannot_call_a_tool_that_was_not_offered(fusion, make_session, backend, monkeypatch):
    monkeypatch.setattr(backend, "pick_tools", lambda query, state, known, limit: ["create_hole", "undo"])
    session = make_session(toolset="router")
    session.set_goal("Ø6 hole at (10, 0)")
    fusion.design["bodies"] = ["Body1"]
    session.run(auto=False)
    assert session.fake_model.steps[0]["tools"] == ["create_hole", "undo"]
    with pytest.raises(ActionError, match="not offered"):
        session.approve(0, FILLET)
    session.approve(0)
    session.close()
    assert [tool["name"] for tool in read_rows(session.log.path)[0]["tools"]] == ["create_hole", "undo"]


def test_router_names_are_sent_and_logged_in_order(fusion, make_session, backend):
    if not backend.has_router:
        pytest.skip("this checkout has no backend/router.py")

    def answer(query, system, names):
        assert names and len(names) <= 5 and len(set(names)) == len(names)
        return {"calls": HOLE} if "create_hole" in names else {"calls": []}

    session = make_session({"Ø6 hole at (10, 0)": answer, "plate 40x30x10": ANSWERS["plate 40x30x10"]},
                           toolset="auto")
    session.set_goal("plate 40x30x10 → Ø6 hole at (10, 0)")
    session.run(auto=True)
    sent = session.fake_model.steps[1]["tools"]
    assert "create_hole" in sent
    assert session.steps[1].status == "done"
    session.close()
    assert [tool["name"] for tool in read_rows(session.log.path)[1]["tools"]] == sent


def test_without_a_router_tools_are_omitted_and_the_row_still_holds_the_gold_tools(
        fusion, make_session, backend, monkeypatch, catalogue):
    monkeypatch.setattr(backend, "router_module", None)
    session = make_session(toolset="auto")
    session.set_goal("fillet the top edges R2")
    fusion.design["bodies"] = ["Body1"]
    session.run(auto=True)
    assert session.fake_model.steps[0]["tools"] is None
    session.close()
    row = read_rows(session.log.path)[0]
    names = [tool["name"] for tool in row["tools"]]
    assert names[0] == "fillet" and len(names) == min(5, len(catalogue)) and len(set(names)) == len(names)


def test_open_dialog_blocks_then_continues(fusion, make_session):
    session = make_session()
    session.set_goal("plate 40x30x10")
    fusion.dialog = "Fillet"
    session.run(auto=True)
    step = session.steps[0]
    assert step.status == "dialog" and step.dialog == "Fillet"
    assert tools_run(fusion) == []                          # no script was sent into the dialog
    with pytest.raises(ActionError):
        session.run(auto=True)
    fusion.dialog = None
    session.resume(0)
    assert step.status == "done" and len(tools_run(fusion)) == 3


def test_undo_last_step_withdraws_its_row(fusion, make_session):
    session = make_session()
    session.set_goal("plate 40x30x10 → Ø6 hole at (10, 0)")
    session.run(auto=True)
    assert session.snapshot()["can_undo_step"]
    session.undo_last_step()                                # the hole: one call, one undo
    assert fusion.undos == 1 and fusion.design["last_feature"] == "Extrude1"
    assert [step.status for step in session.steps] == ["done", "pending"]
    session.undo_last_step()                                # the plate: three calls; its row is already written
    assert fusion.undos == 4 and fusion.design["bodies"] == []
    assert "already line 1" in " ".join(session.steps[0].notes)
    session.close()
    rows = read_rows(session.log.path)
    assert [row["query"] for row in rows] == ["plate 40x30x10"]         # the hole's row was never written
    with pytest.raises(ActionError):
        session.undo_last_step()


def test_images_are_kept_for_the_ui(fusion, make_session, backend, monkeypatch):
    shot = {"name": "fusion_mcp_read", "arguments": {"queryType": "screenshot", "direction": "front"}}
    real = backend.render
    monkeypatch.setattr(backend, "render", lambda name, args, export_dir=None:
                        shot if name == "fillet" else real(name, args, export_dir))
    monkeypatch.setattr(backend, "call_info", lambda name: {"confirm": False, "read_only": name == "fillet",
                                                            "undoable": None})
    session = make_session()
    session.set_goal("fillet the top edges R2")
    fusion.design["bodies"] = ["Body1"]
    session.run(auto=True)
    result = session.steps[0].results[0]
    assert result["ok"] and len(result["images"]) == 1 and result["via"] == "fusion_mcp_read"
    assert session.images[result["images"][0]["id"]]["mime"] == "image/png"
    assert "images" in json.dumps(session.snapshot())       # the snapshot carries ids, not the bytes
    assert session.images[result["images"][0]["id"]]["data"] not in json.dumps(session.snapshot())


def test_export_dir_is_passed_only_when_the_renderer_takes_it():
    assert Backend._has_parameter(lambda name, arguments, export_dir=None: None, "export_dir")
    assert Backend._has_parameter(lambda name, arguments, **extra: None, "export_dir")
    assert not Backend._has_parameter(lambda name, arguments: None, "export_dir")


def _has(catalogue, *names):
    known = {tool["name"] for tool in catalogue}
    return all(name in known for name in names)


def test_unreadable_dialog_check_means_do_not_send(fusion, make_session, backend, monkeypatch):
    if backend.result_module is None:
        pytest.skip("this checkout has no backend/mcp_result.py")

    def unreadable(_answer):
        raise ValueError("unknown activeCommand answer shape")

    monkeypatch.setattr(backend.result_module, "dialog_open", unreadable)
    session = make_session()
    session.set_goal("plate 40x30x10")
    session.run(auto=True)
    assert session.steps[0].status == "dialog" and "could not be read" in session.steps[0].dialog
    assert tools_run(fusion) == []


def test_open_document_asks_first_then_lets_the_user_pick(fusion, make_session, backend, catalogue):
    if not _has(catalogue, "open_document") or not hasattr(backend.render_module, "open_document_call"):
        pytest.skip("this catalogue has no open_document")
    answers = {"plate 40x30x10": ANSWERS["plate 40x30x10"],
               "open Bracket": {"calls": [{"name": "open_document", "arguments": {"name": "Bracket"}}]},
               "Ø6 hole at (10, 0)": ANSWERS["Ø6 hole at (10, 0)"]}
    session = make_session(answers)
    session.set_goal("plate 40x30x10 → open Bracket → Ø6 hole at (10, 0)")
    session.run(auto=True)
    step = session.steps[1]
    assert step.status == "proposed" and step.confirm == ["open_document needs your confirmation"]
    assert fusion.opened == []
    session.approve(1)
    assert step.status == "choice" and [entry["name"] for entry in step.choice] == ["Bracket v3", "Bracket v4"]
    with pytest.raises(ActionError):
        session.choose(1, "urn:something:else")             # only an id Fusion returned
    session.choose(1, step.choice[1]["id"])
    assert fusion.opened == ["urn:adsk.wipprod:dm.lineage:BBB"]
    # the new document is empty, so the hole is held back by the guard; nothing is rolled back across the switch
    assert step.status == "done" and session.steps[2].status == "proposed" and session.steps[2].blocked
    assert [issue["code"] for issue in session.steps[2].guard["issues"]] == ["state_need"]
    assert fusion.undos == 0 and not session.snapshot()["can_undo_step"]
    assert session.steps[2].state_line == EMPTY_STATE


def test_capture_view_returns_an_image_without_a_dialog_check(fusion, make_session, backend, catalogue):
    if not _has(catalogue, "capture_view") or not backend.call_info("capture_view")["read_only"]:
        pytest.skip("this catalogue has no read-only capture_view")
    session = make_session({"show me the front view": {"calls": [{"name": "capture_view",
                                                                 "arguments": {"direction": "front"}}]}})
    session.set_goal("show me the front view")
    fusion.dialog = "Extrude"                               # read-only tools run even with a dialog open
    session.run(auto=True)
    step = session.steps[0]
    assert step.status == "done" and len(step.results[0]["images"]) == 1
    assert not any(call["arguments"].get("queryType") == "activeCommand" for call in fusion.calls)
    session.undo_last_step()
    assert fusion.undos == 0                                # nothing to take back for a read-only call


def test_export_goes_to_the_apps_folder(fusion, make_session, backend, catalogue, tmp_path):
    if not _has(catalogue, "export_model"):
        pytest.skip("this catalogue has no export_model")
    arguments = {}
    for key in backend.render_module.catalogue()["export_model"]["parameters"].get("required", []):
        spec = backend.render_module.catalogue()["export_model"]["parameters"]["properties"][key]
        arguments[key] = spec["enum"][0] if "enum" in spec else "part"
    request = "export " + " ".join(str(value) for value in arguments.values())      # the values are in the text
    session = make_session({request: {"calls": [{"name": "export_model", "arguments": arguments}]}})
    session.set_goal(request)
    fusion.design["bodies"] = ["Body1"]
    session.run(auto=True)
    assert session.steps[0].status == "done", session.steps[0].error
    script = fusion.scripts[-2]["script"] if fusion.scripts[-1]["tool"] == "state" else fusion.scripts[-1]["script"]
    assert f"EXPORT_DIR = {str(tmp_path / 'exports')!r}" in script
    assert (tmp_path / "exports").is_dir()
    session.undo_last_step()
    assert fusion.undos == 0                                # an export leaves no undo step
