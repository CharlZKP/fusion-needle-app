"""Start the real app headless (a child process) against the fake Fusion and a fake model
server, drive it through its HTTP API the way the page does, and shut it down."""
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest
from .kit import ANSWERS, DESKTOP, GOAL, read_rows
from .fake_model import FakeModel

OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def http(url, body=None, token=None, host=None):
    headers = {}
    if token:
        headers["X-FN-Token"] = token
    if host:
        headers["Host"] = host
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers)
    try:
        with OPENER.open(request, timeout=20) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as failure:
        return failure.code, failure.headers, failure.read()


@pytest.fixture
def running_app(fusion, catalogue, tmp_path):
    answers = dict(ANSWERS)
    answers["take a picture"] = {"calls": []}
    answers["4 holes on the corner with 5 mm margin"] = {"calls": [{"name": "create_hole",
                                                                    "arguments": {"diameter": 4, "depth": 5}}]}
    answers["fillet -2 mm"] = {"calls": [{"name": "fillet", "arguments": {"radius": -2}}]}
    if any(tool["name"] == "capture_view" for tool in catalogue):
        answers["show me the front view"] = {"calls": [{"name": "capture_view", "arguments": {"direction": "front"}}]}
    model = FakeModel(catalogue, answers, token="tok").start()
    config = tmp_path / "config"
    config.mkdir()
    (config / "settings.json").write_text(json.dumps({"log_dir": str(tmp_path / "history"), "toolset": "catalogue",
                                                      "save_history": True}), encoding="utf-8")
    process = subprocess.Popen(
        [sys.executable, str(DESKTOP / "fusion_needle.py"), "--headless", "--config-dir", str(config),
         "--fusion-url", fusion.url, "--model-url", model.url, "--model-token", "tok"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        line = process.stdout.readline()
        found = re.search(r"http://127\.0\.0\.1:\d+/", line)
        assert found, f"no URL in the first line: {line!r}"
        yield {"url": found.group(0), "process": process, "model": model, "log_dir": tmp_path / "history",
               "config": config}
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(10)
            except subprocess.TimeoutExpired:
                process.kill()
        model.stop()


def wait_for(check, timeout=30.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.2)
    raise AssertionError("timed out")


def test_headless_app_serves_the_ui_and_runs_a_goal(running_app, fusion):
    url = running_app["url"]
    status, headers, page = http(url)
    assert status == 200 and headers["Content-Type"].startswith("text/html")
    page = page.decode()
    assert "<title>Fusion Needle</title>" in page and "__FN_TOKEN__" not in page
    token = re.search(r'name="fn-token" content="([^"]+)"', page).group(1)
    assert "script-src 'self'" in headers["Content-Security-Policy"]

    # everything the page loads is served locally; nothing points at the network
    for asset in re.findall(r'(?:src|href)="(/ui/[^"]+)"', page):
        status, _headers, body = http(url.rstrip("/") + asset)
        assert status == 200 and body, asset
        text = body.decode()
        assert not re.findall(r"https?://(?!www\.w3\.org/2000/svg|127\.0\.0\.1)", text), asset
    assert not re.findall(r"https?://", page)
    assert http(url + "ui/../server.py")[0] == 404

    # the API needs the page's token and a loopback Host
    assert http(url + "api/state")[0] == 401
    assert http(url + "api/state", token="wrong")[0] == 401
    assert http(url + "api/goal", body={"goal": "x"})[0] == 401
    assert http(url + "api/state", token=token, host="evil.example")[0] == 403

    def state():
        status, _headers, body = http(url + "api/state", token=token)
        assert status == 200
        return json.loads(body)

    ready = wait_for(lambda: (lambda s: s if s["model"]["state"] == "ready"
                              and s["fusion"]["state"] == "connected" else None)(state()))
    assert ready["fusion"]["server"] == "MCP Server Adapter 1.0.0"
    assert ready["model"]["model"] == "fake-20L.cact" and ready["backend"]["ok"]
    assert "token" not in ready["settings"]["model"]
    tools = json.loads(http(url + "api/tools", token=token)[2])["tools"]
    assert len(tools) == ready["model"]["tools"] and all("info" in tool for tool in tools)
    wait_for(lambda: state()["session"]["fusion_state"].startswith("units: mm"))

    status, _headers, body = http(url + "api/split", body={"goal": GOAL}, token=token)
    assert json.loads(body)["features"] == list(ANSWERS)
    assert http(url + "api/run", body={"auto": True}, token=token)[0] == 400         # no goal yet
    assert http(url + "api/goal", body={"goal": GOAL + "\ntake a picture"}, token=token)[0] == 200
    assert http(url + "api/run", body={"auto": True}, token=token)[0] == 200
    final = wait_for(lambda: (lambda s: s if not s["session"]["busy"] and s["session"]["current"] == 4
                              else None)(state()))
    steps = final["session"]["steps"]
    assert [step["status"] for step in steps] == ["done"] * 4 + ["empty"]
    assert steps[0]["results"][2]["ok"] and steps[0]["row_status"] == "logged"
    assert steps[3]["row_status"] == "pending"
    assert [entry["tool"] for entry in fusion.modelling_scripts()] == [
        "create_sketch", "draw_rectangle", "extrude", "create_hole", "circular_pattern", "fillet"]
    script = json.loads(http(url + "api/script?step=0&call=1", token=token)[2])
    assert script["tool"] == "fusion_mcp_execute" and "ARGS = {" in script["script"]
    assert http(url + "api/accept_empty", body={"index": 4, "category": "nocall_offtopic"}, token=token)[0] == 200
    wait_for(lambda: state()["session"]["finished"])

    # a second goal; a result with an image is served to the page (and only with the token)
    expected = ["multi", "create_hole", "circular_pattern", "fillet", "nocall_offtopic"]
    if "show me the front view" in running_app["model"].answers:
        assert http(url + "api/goal", body={"goal": "show me the front view"}, token=token)[0] == 200
        assert http(url + "api/run", body={"auto": True}, token=token)[0] == 200
        shown = wait_for(lambda: (lambda s: s if s["session"]["finished"] and not s["session"]["busy"]
                                  else None)(state()))
        image = shown["session"]["steps"][0]["results"][0]["images"][0]
        status, headers, data = http(url + f"api/image/{image['id']}?k={token}")
        assert status == 200 and headers["Content-Type"] == "image/png" and data[:8] == b"\x89PNG\r\n\x1a\n"
        assert http(url + f"api/image/{image['id']}")[0] == 401
        expected.append("capture_view")

    # quit: the pending row is written, the process ends
    assert http(url + "api/quit", body={}, token=token)[0] == 200
    assert running_app["process"].wait(20) == 0
    files = list(running_app["log_dir"].glob("*.jsonl"))
    assert len(files) == 1 and re.fullmatch(r"\d{4}-\d\d-\d\d-\d{4}(-\d+)?\.jsonl", files[0].name)
    rows = read_rows(files[0])
    assert [row["meta"]["category"] for row in rows] == expected
    assert len({row["meta"]["goal_id"] for row in rows}) == (2 if len(expected) == 6 else 1)
    assert all(body["tools"] is None for body in running_app["model"].steps)        # toolset: catalogue


def test_headless_app_does_not_send_a_held_back_call_to_fusion(running_app, fusion):
    """The real app over HTTP: the guard holds a call back, Fusion never sees it, Run anyway is the user's call."""
    url = running_app["url"]
    token = re.search(r'name="fn-token" content="([^"]+)"', http(url)[2].decode()).group(1)

    def state():
        return json.loads(http(url + "api/state", token=token)[2])

    def post(action, body=None):
        status, _headers, answer = http(url + "api/" + action, body=body or {}, token=token)
        return status, json.loads(answer)

    def settled(current):
        return wait_for(lambda: (lambda s: s if not s["session"]["busy"] and s["session"]["current"] == current
                                 and s["session"]["steps"][current]["status"] == "proposed" else None)(state()))

    wait_for(lambda: state()["model"]["state"] == "ready" and state()["fusion"]["state"] == "connected")
    panel = json.loads(http(url + "api/help", token=token)[2])                       # "What can I say?"
    assert http(url + "api/help")[0] == 401
    assert sum(len(group["tools"]) for group in panel["groups"]) == state()["model"]["tools"] and panel["starters"]
    assert any(tool["examples"] for group in panel["groups"] for tool in group["tools"])

    goal = "plate 40x30x10 -> 4 holes on the corner with 5 mm margin -> fillet -2 mm"
    assert post("goal", {"goal": goal})[0] == 200 and post("run", {"auto": True})[0] == 200
    held = settled(1)["session"]["steps"][1]
    assert held["blocked"] and held["guard"]["decision"] == "blocked" and held["guard"]["overridable"]
    assert held["calls"][0]["arguments"] == {"diameter": 4, "depth": 5}              # shown, with the reason
    assert "count" in held["guard"]["issues"][0]["message"] and held["help"]["examples"]
    ran = [entry["tool"] for entry in fusion.modelling_scripts()]
    assert ran == ["create_sketch", "draw_rectangle", "extrude"]                     # the hole was never sent
    assert post("run", {"auto": True})[0] == 400 and post("approve", {"index": 1})[0] == 400
    assert post("approve", {"index": 1, "override": "yes"})[0] == 400                # only a real true overrides
    assert [entry["tool"] for entry in fusion.modelling_scripts()] == ran

    assert post("approve", {"index": 1, "override": True})[0] == 200                 # Run anyway
    last = settled(2)["session"]
    assert last["steps"][1]["status"] == "done" and last["steps"][1]["row_status"] == "not_logged"
    assert [entry["tool"] for entry in fusion.modelling_scripts()] == ran + ["create_hole"]
    schema = last["steps"][2]
    assert schema["blocked"] and not schema["guard"]["overridable"]                  # out of bounds: no Run anyway
    status, answer = post("approve", {"index": 2, "override": True})
    assert status == 400 and "greater than 0" in answer["error"]
    assert [entry["tool"] for entry in fusion.modelling_scripts()] == ran + ["create_hole"]
    assert last["guard_counts"]["blocked"] == 2 and last["guard_counts"]["overridden"] == 1

    status, out = post("diagnostics_export")                                         # the decisions are in the export
    assert status == 200
    import zipfile
    with zipfile.ZipFile(out["path"]) as archive:
        events = [json.loads(line) for line in archive.read("events-this-launch.jsonl").decode().splitlines()]
        session = json.loads(archive.read("session.json"))
    assert [(event["decision"], event["step"]) for event in events if event["kind"] == "guard"] == [
        ("blocked", 1), ("overridden", 1), ("blocked", 2)]
    assert session["steps"][2]["guard"]["issues"][0]["code"] == "out_of_range"
    assert read_rows(running_app["log_dir"] / (state()["session"]["log"]["session"] + ".jsonl"))[0]["query"] == \
        "plate 40x30x10"
