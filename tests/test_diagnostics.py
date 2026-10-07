"""Evidence for the first session against a real Fusion: the unreadable dialog check
(shown, logged, overridable per step), "Check Fusion", the diagnostics export, and the
image options of capture_view."""
import json
import re
import subprocess
import sys
import zipfile

import pytest

from app.backend import Backend
from app.diagnostics import Diagnostics, raw_text, scrub, shape
from app.mcp import FusionClient
from app.selfcheck import CHECK_IMAGE, judge_orientation, png_size, run_check
from app.session import ActionError

from .fake_fusion import PNG_1X1, FakeFusion, png
from .fake_model import FakeModel
from .kit import ANSWERS, DESKTOP
from .test_app_http import http, wait_for

UNKNOWN = {"command": {"id": "SketchCreate", "label": "Create Sketch"}, "modal": False}


def tools_run(fusion):
    return [entry["tool"] for entry in fusion.modelling_scripts()]


def events(directory):
    path = directory / "events.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# ---- the dialog check answers in a shape nobody has seen --------------------------------

def test_unknown_active_command_answer_stops_shows_the_raw_answer_and_logs_it(fusion, make_session, tmp_path):
    fusion.active_command_answer = UNKNOWN
    session = make_session()
    session.diagnostics = Diagnostics(tmp_path / "diagnostics")
    session.set_goal("plate 40x30x10 -> Ø6 hole at (10, 0)")
    session.run(auto=True)
    step = session.steps[0]
    assert step.status == "dialog" and step.dialog_unknown and "could not be read" in step.dialog
    assert tools_run(fusion) == []                          # fail closed: nothing was sent
    assert json.loads(step.dialog_raw)["content"][0]["text"] == json.dumps(UNKNOWN)     # what Fusion really sent
    shown = step.to_dict()
    assert shown["dialog_unknown"] and "SketchCreate" in shown["dialog_raw"] and not shown["dialog_override"]
    logged = events(tmp_path / "diagnostics")
    assert [entry["kind"] for entry in logged] == ["dialog_check_unreadable"]
    assert logged[0]["call"] == "create_sketch" and logged[0]["feature"] == "plate 40x30x10"
    assert "SketchCreate" in json.dumps(logged[0]["answer"]) and logged[0]["reason"]

    session.resume(0)                                       # "Check again": same answer, still stopped
    assert step.status == "dialog" and tools_run(fusion) == []

    session.proceed(0)                                      # the user looked at Fusion and takes the risk
    assert step.status == "done" and step.dialog_override
    assert tools_run(fusion) == ["create_sketch", "draw_rectangle", "extrude"]
    assert any("proceed" in note for note in step.notes) and step.row_status == "logged"
    assert [entry["kind"] for entry in events(tmp_path / "diagnostics")] == [
        "dialog_check_unreadable", "dialog_check_unreadable", "dialog_check_overridden",
        "dialog_check_unreadable"]                          # first stop, "check again", the override, the next step

    following = session.steps[1]                            # the choice covered that step only
    assert following.status == "dialog" and following.dialog_unknown and not following.dialog_override
    assert tools_run(fusion) == ["create_sketch", "draw_rectangle", "extrude"]


@pytest.mark.parametrize("answer", ["Select [default]", {"activeCommand": "Extrude"}, {}])
def test_other_unreadable_answers_also_fail_closed(fusion, make_session, answer):
    fusion.active_command_answer = answer
    session = make_session()
    session.set_goal("Ø6 hole at (10, 0)")
    fusion.design["bodies"] = ["Body1"]
    session.run(auto=True)
    step = session.steps[0]
    assert step.status == "dialog" and step.dialog_unknown and step.dialog_raw and tools_run(fusion) == []


def test_proceed_is_only_for_an_unreadable_answer(fusion, make_session):
    fusion.dialog = "Fillet"                                # a readable answer that names an open dialog
    session = make_session()
    session.set_goal("plate 40x30x10")
    session.run(auto=True)
    step = session.steps[0]
    assert step.status == "dialog" and step.dialog == "Fillet" and not step.dialog_unknown
    with pytest.raises(ActionError):
        session.proceed(0)
    assert tools_run(fusion) == []
    fusion.dialog = None
    session.resume(0)
    assert step.status == "done"
    with pytest.raises(ActionError):
        session.proceed(0)


def test_override_does_not_outlive_a_retry_and_a_real_dialog_still_fails_safely(fusion, make_session):
    fusion.active_command_answer = UNKNOWN
    fusion.dialog = "Extrude"                               # ... and a dialog really is open
    session = make_session()
    session.set_goal("plate 40x30x10")
    session.run(auto=True)
    session.proceed(0)
    step = session.steps[0]
    assert step.status == "failed" and "Extrude" in step.error      # Fusion refused the script; nothing is left
    assert fusion.design["bodies"] == [] and step.row_status == ""
    fusion.dialog = None
    session.retry(0)
    assert step.status == "dialog" and step.dialog_unknown and not step.dialog_override     # asked again


def test_dialog_check_reads_known_shapes_and_flags_the_rest(backend):
    def result(data, ok=True):
        raw = {"content": [{"type": "text", "text": json.dumps(data)}]}
        return {"ok": ok, "data": data, "raw": raw, "error": "" if ok else "boom"}

    idle = backend.dialog_check(result({"commandId": "SelectCommand", "commandName": "Select",
                                        "isDefaultCommand": True}))
    assert idle == {"open": False, "name": None, "unknown": False, "reason": ""}
    assert backend.dialog_check(result({"activeCommand": None}))["open"] is False
    busy = backend.dialog_check(result({"commandId": "Extrude", "commandName": "Extrude", "isDefaultCommand": False}))
    assert busy["open"] and busy["name"] == "Extrude" and not busy["unknown"]
    assert backend.dialog_check(result(UNKNOWN))["unknown"]
    failed = backend.dialog_check(result({"commandName": "Select", "isDefaultCommand": True}, ok=False))
    assert failed["unknown"] and "boom" in failed["reason"]     # a failed query proves nothing
    assert backend.dialog_open(result(UNKNOWN)).startswith("unknown:")


# ---- Check Fusion -----------------------------------------------------------------------

def by_id(report):
    return {item["id"]: item for item in report["items"]}


def test_check_fusion_passes_on_a_z_up_install_and_only_reads(fusion, backend):
    kept = []
    report = run_check(FusionClient(fusion.url), backend, keep_images=lambda images: kept.extend(images) or [{"id": "i"}])
    items = by_id(report)
    assert list(items) == ["initialize", "tools", "state", "active_command", "screenshot", "up_axis"]
    assert {item["status"] for item in report["items"]} == {"pass"} and report["overall"] == "pass"
    assert "MCP Server Adapter 1.0.0" in items["initialize"]["detail"]
    assert items["state"]["detail"] == "units: mm; sketch: none; bodies: none; last_feature: none"
    assert f"PNG {CHECK_IMAGE[0]}x{CHECK_IMAGE[1]}" in items["screenshot"]["detail"] and len(kept) == 1
    assert report["summary"]["orientation"]["document_up"] == "+z" and "Z up" in items["up_axis"]["detail"]
    assert not any("raw" in item for item in report["items"])
    # read-only all the way: no modelling script, every script flagged readOnly, the camera is left alone
    assert fusion.modelling_scripts() == [] and fusion.undos == 0
    assert {entry["tool"] for entry in fusion.scripts} == {"state", "orientation"}
    shot = [call["arguments"] for call in fusion.calls if call["arguments"].get("queryType") == "screenshot"]
    assert shot == [{"queryType": "screenshot", "direction": "current", "width": 320, "height": 180,
                     "transparentBackground": False}]
    assert [message["method"] for message in fusion.requests][:3] == ["initialize", "notifications/initialized",
                                                                       "tools/list"]


def test_check_fusion_flags_a_y_up_document_before_anything_is_built(fusion, backend):
    fusion.orientation.update(default_modeling_orientation="YUpModelingOrientation", front_up=[0.0, 1.0, 0.0],
                              front_eye=[0.0, 0.0, -1.0], camera_up=[0.0, 1.0, 0.0])
    report = run_check(FusionClient(fusion.url), backend)
    item = by_id(report)["up_axis"]
    assert item["status"] == "fail" and report["overall"] == "fail"
    assert "+y up" in item["detail"] and "Z up" in item["detail"] and "Default modeling orientation" in item["detail"]
    assert item["values"]["document_up"] == "+y" and item["values"]["preference_up"] == "y"
    assert json.loads(item["raw"])["front_up"] == [0.0, 1.0, 0.0]       # the raw values go back with the report


def test_orientation_verdicts():
    def verdict(**data):
        return judge_orientation(data)[0]

    z, y = "ZUpModelingOrientation", "YUpModelingOrientation"
    assert verdict(default_modeling_orientation=z, front_up=[0, 0, 1]) == "pass"
    assert verdict(default_modeling_orientation=y, front_up=[0, 0, 1]) == "warn"     # new documents will be Y up
    assert verdict(default_modeling_orientation=z, front_up=[0, 1, 0]) == "fail"     # this document is Y up
    assert verdict(front_up=[0, 0, 1], errors={"default_modeling_orientation": "AttributeError"}) == "pass"
    assert verdict(default_modeling_orientation=z, errors={"front_view": "AttributeError"}) == "warn"
    assert verdict(default_modeling_orientation=y) == "fail"
    assert verdict(errors={"front_view": "x", "default_modeling_orientation": "y"}) == "fail"
    assert verdict(default_modeling_orientation=z, front_up=[0, 0, -1]) == "fail"    # upside down is not Z up
    assert verdict(default_modeling_orientation=z, front_up=[0.5, 0.5, 0.7]) == "warn"   # no axis: unreadable


def test_check_fusion_reports_failures_with_the_raw_shape(fusion, backend):
    fusion.active_command_answer = UNKNOWN
    fusion.ignore_image_size = True
    fusion.fail_tools["orientation"] = "AttributeError: 'Viewport' object has no attribute 'frontUpDirection'"
    report = run_check(FusionClient(fusion.url), backend)
    items = by_id(report)
    assert report["overall"] == "fail"
    assert items["initialize"]["status"] == items["tools"]["status"] == items["state"]["status"] == "pass"
    command = items["active_command"]
    assert command["status"] == "fail" and "SketchCreate" in command["raw"]
    assert command["shape"] == {"content": [{"type": "str", "text": "str"}]}
    assert items["screenshot"]["status"] == "warn" and "1x1" in items["screenshot"]["detail"]
    assert items["up_axis"]["status"] == "fail" and "frontUpDirection" in items["up_axis"]["raw"]
    assert "view cube" in items["up_axis"]["detail"]


def test_check_fusion_with_an_open_dialog_and_the_json_text_screenshot(backend):
    fake = FakeFusion(image_blocks=False).start()
    try:
        fake.dialog = "Extrude"
        report = run_check(FusionClient(fake.url), backend)
        items = by_id(report)
        assert items["active_command"]["status"] == "warn" and "Extrude" in items["active_command"]["detail"]
        assert items["screenshot"]["status"] == "pass" and "JSON text" in items["screenshot"]["detail"]
        assert items["state"]["status"] == "pass"           # read-only scripts run with a dialog open
        assert report["overall"] == "warn"
    finally:
        fake.stop()


def test_check_fusion_without_fusion(backend):
    report = run_check(FusionClient("http://127.0.0.1:9/mcp", timeout=3), backend)
    statuses = [item["status"] for item in report["items"]]
    assert statuses == ["fail"] + ["skip"] * 5 and report["overall"] == "fail"
    assert "Is Fusion running" in report["items"][0]["detail"]


def test_png_helpers():
    assert png_size(png(320, 180))[:2] == (320, 180) and png_size(PNG_1X1)[:2] == (1, 1)
    assert png_size("bm90IGEgcG5n") is None and png_size("***") is None


# ---- the record and the export -----------------------------------------------------------

def test_scrub_replaces_images_by_their_size_and_keeps_everything_else():
    big = png(64, 64) * 3
    answer = {"result": {"content": [{"type": "image", "data": big, "mimeType": "image/png"},
                                     {"type": "text", "text": json.dumps({"type": "image", "base64Data": big})},
                                     {"type": "text", "text": "{\"message\": \"ok\", \"success\": true}"}]},
              "script": "def run(_context: str):\n    pass\n" * 40, "data": "short"}
    cleaned = scrub(answer)
    text = json.dumps(cleaned)
    assert big not in text and text.count("base64 removed") == 2 and f"{len(big)} characters" in text
    assert cleaned["script"] == answer["script"] and cleaned["data"] == "short"
    assert cleaned["result"]["content"][2] == answer["result"]["content"][2]
    assert answer["result"]["content"][0]["data"] == big                # the original is not touched
    assert "base64 removed" in raw_text(answer) and shape({"a": [1], "b": {"c": None}}) == {
        "a": ["int"], "b": {"c": "NoneType"}}


def test_the_ring_keeps_the_last_n_calls_and_counts_repeated_failures(fusion, backend, tmp_path):
    diagnostics = Diagnostics(tmp_path / "d", keep=10)
    client = FusionClient(fusion.url, recorder=diagnostics.record_call)
    for _ in range(3):
        client.alive()                                      # the keep-alive ping is not recorded
    for _ in range(12):
        client.call(backend.state_call())
    assert len(diagnostics.calls) == 10 and diagnostics.total_calls == 14       # initialize, notification, 12 calls
    last = diagnostics.calls[-1]
    assert last["method"] == "tools/call" and last["http_status"] == 200 and last["ms"] >= 0
    assert "def run(_context: str):" in last["request"]["arguments"]["object"]["script"]
    assert json.loads(last["response"]["result"]["content"][0]["text"])["success"] is True
    summary = diagnostics.summary()
    assert summary["calls"][-1]["tool"] == "fusion_mcp_execute" and summary["calls"][-1]["kind"] == "script"

    offline = Diagnostics(tmp_path / "e", keep=10)
    dead = FusionClient("http://127.0.0.1:9/mcp", timeout=2, recorder=offline.record_call)
    for _ in range(4):
        assert not dead.alive()
    assert len(offline.calls) == 1 and offline.calls[0]["repeats"] == 3 and "McpUnreachable" in offline.calls[0]["error"]


@pytest.fixture
def app_for_export(fusion, catalogue, tmp_path):
    answers = dict(ANSWERS)
    answers["show me the front view"] = {"calls": [{"name": "capture_view", "arguments": {"direction": "front"}}]}
    model = FakeModel(catalogue, answers, token="tok-SERVER-SECRET-123").start()
    config = tmp_path / "config"
    config.mkdir()
    (config / "settings.json").write_text(json.dumps({
        "log_dir": str(tmp_path / "history"), "toolset": "catalogue", "save_history": True, "capture_width": 640,
        "capture_height": 360, "model": {"license_key": "LICENCE-KEY-SECRET-456"}}), encoding="utf-8")
    process = subprocess.Popen(
        [sys.executable, str(DESKTOP / "fusion_needle.py"), "--headless", "--config-dir", str(config),
         "--fusion-url", fusion.url, "--model-url", model.url, "--model-token", "tok-SERVER-SECRET-123"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        url = re.search(r"http://127\.0\.0\.1:\d+/", process.stdout.readline()).group(0)
        token = re.search(r'name="fn-token" content="([^"]+)"', http(url)[2].decode()).group(1)
        yield {"url": url, "token": token, "config": config, "process": process}
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(10)
            except subprocess.TimeoutExpired:
                process.kill()
        model.stop()


def test_app_check_fusion_then_export_a_zip_without_secrets(app_for_export, fusion):
    url, token = app_for_export["url"], app_for_export["token"]

    def state():
        return json.loads(http(url + "api/state", token=token)[2])

    def post(action, body=None):
        status, _headers, data = http(url + "api/" + action, body=body or {}, token=token)
        return status, json.loads(data)

    wait_for(lambda: (lambda s: s["model"]["state"] == "ready" and s["fusion"]["state"] == "connected"
                      and not s["session"]["busy"])(state()))
    assert http(url + "api/check_fusion", body={})[0] == 401            # the page's token is needed
    assert post("check_fusion")[0] == 200
    checked = wait_for(lambda: (lambda s: s if s["fusion_check"] and not s["fusion_check"].get("running")
                                and not s["session"]["busy"] else None)(state()))
    check = checked["fusion_check"]
    assert check["overall"] == "pass" and [item["id"] for item in check["items"]][-1] == "up_axis"
    image = by_id(check)["screenshot"]["images"][0]
    assert http(url + f"api/image/{image['id']}?k={token}")[1]["Content-Type"] == "image/png"

    # a step with an image, at the size the settings ask for (item 4), so the record holds one
    assert post("goal", {"goal": "plate 40x30x10 -> show me the front view"})[0] == 200
    assert post("run", {"auto": True})[0] == 200
    wait_for(lambda: (lambda s: s["session"]["finished"] and not s["session"]["busy"])(state()))
    shots = [call["arguments"] for call in fusion.calls if call["arguments"].get("queryType") == "screenshot"]
    assert shots[-1] == {"queryType": "screenshot", "direction": "front", "width": 640, "height": 360,
                         "transparentBackground": False}

    listing = json.loads(http(url + "api/diagnostics", token=token)[2])
    assert listing["calls"][-1]["tool"] and listing["total"] >= len(listing["calls"]) > 5
    assert http(url + "api/diagnostics")[0] == 401

    status, out = post("diagnostics_export")
    assert status == 200 and out["path"].endswith(".zip")
    assert out["folder"] == str(app_for_export["config"] / "diagnostics")
    assert state()["diagnostics"]["last_export"] == out["path"]
    with zipfile.ZipFile(out["path"]) as archive:
        names = set(archive.namelist())
        files = {name: archive.read(name).decode("utf-8") for name in names}
    assert {"README.txt", "info.json", "mcp-calls.jsonl", "events.jsonl", "fusion-check.json", "session.json",
            "model-server.log"} <= names
    everything = "\n".join(files.values())
    for secret in ("tok-SERVER-SECRET-123", "LICENCE-KEY-SECRET-456", token):
        assert secret not in everything
    info = json.loads(files["info.json"])
    assert info["app"]["version"] and info["engine"]["needle_version"] == "3.1.1"
    assert info["engine"]["model"] == "fake-20L.cact" and info["fusion"]["server_info"]["name"] == "MCP Server Adapter"
    assert info["settings"]["model"]["token_set"] is True and "token" not in info["settings"]["model"]
    # the settings file of this test still holds a licence key of an older version: loaded, dropped, not exported
    assert not [key for key in info["settings"]["model"] if "license" in key or "bundle" in key]
    assert info["settings"]["capture_width"] == 640 and info["platform"] and info["python"]
    calls = [json.loads(line) for line in files["mcp-calls.jsonl"].splitlines()]
    methods = [call["method"] for call in calls]
    assert methods[:2] == ["initialize", "notifications/initialized"] and "tools/list" in methods
    scripts = [call["request"]["arguments"]["object"]["script"] for call in calls
               if call["method"] == "tools/call" and call["request"]["arguments"].get("featureType") == "script"]
    assert any("# extrude: rendered by" in script and "ARGS = {" in script for script in scripts)     # scripts in full
    pictures = [call for call in calls if (call["request"] or {}).get("arguments", {}).get("queryType") == "screenshot"]
    assert len(pictures) == 2
    for picture in pictures:
        block = picture["response"]["result"]["content"][0]
        assert block["type"] == "image" and block["data"].startswith("<base64 removed:")
    assert not re.search(r"[A-Za-z0-9+/]{300,}", everything)            # no image data anywhere
    assert json.loads(files["fusion-check.json"])["overall"] == "pass"
    assert json.loads(files["session.json"])["steps"][0]["status"] == "done"
    kinds = [json.loads(line)["kind"] for line in files["events.jsonl"].splitlines()]
    assert kinds == ["fusion_check"]


# ---- capture_view image options (item 4) -------------------------------------------------

def capture_arguments(fusion, make_session, **settings):
    session = make_session({"show me the top view": {"calls": [{"name": "capture_view",
                                                                "arguments": {"direction": "top"}}]}}, **settings)
    session.set_goal("show me the top view")
    session.run(auto=True)
    assert session.steps[0].status == "done", session.steps[0].error
    arguments = [call["arguments"] for call in fusion.calls if call["arguments"].get("queryType") == "screenshot"][-1]
    image = session.images[session.steps[0].results[0]["images"][0]["id"]]
    return arguments, png_size(image["data"])[:2], session


def test_capture_view_gets_size_and_an_opaque_background_from_the_settings(fusion, make_session, catalogue):
    if not any(tool["name"] == "capture_view" for tool in catalogue):
        pytest.skip("this catalogue has no capture_view")
    arguments, size, session = capture_arguments(fusion, make_session)
    assert arguments == {"queryType": "screenshot", "direction": "top", "width": 1280, "height": 720,
                         "transparentBackground": False}
    assert size == (1280, 720)
    assert session.script(0, 0)["arguments"] == arguments               # "Show what was sent" shows the real call
    arguments, size, _ = capture_arguments(fusion, make_session, capture_width=800, capture_height=99999,
                                           capture_transparent=True)
    assert (arguments["width"], arguments["height"], arguments["transparentBackground"]) == (800, 4096, True)
    arguments, _size, _ = capture_arguments(fusion, make_session, capture_width=1, capture_height="tall")
    assert (arguments["width"], arguments["height"]) == (32, 720)


def test_capture_options_touch_nothing_but_a_screenshot_query():
    options = {"width": 1280, "height": 720, "transparentBackground": False}
    script = {"name": "fusion_mcp_execute", "arguments": {"featureType": "script", "object": {"script": "x"}}}
    search = {"name": "fusion_mcp_read", "arguments": {"queryType": "document", "operation": "search", "name": "a"}}
    assert Backend.with_capture_options(script, options) is script
    assert Backend.with_capture_options(search, options) is search
    shot = {"name": "fusion_mcp_read", "arguments": {"queryType": "screenshot", "direction": "iso-top-right",
                                                     "width": 256}}
    merged = Backend.with_capture_options(shot, options)
    assert merged["arguments"] == {"queryType": "screenshot", "direction": "iso-top-right", "width": 256,
                                   "height": 720, "transparentBackground": False}     # the renderer's value wins
    assert shot["arguments"] == {"queryType": "screenshot", "direction": "iso-top-right", "width": 256}
    assert Backend.with_capture_options(shot, None) is shot
