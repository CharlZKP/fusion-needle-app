"""The model server must not outlive the app (app/supervisor.py), and a server that a
crashed launch left behind is stopped at the next start, but only when it is provably ours.

These run for real on POSIX. The Windows paths (Job Objects, taskkill, process times through
ctypes) are written but have never been run: on Windows these same tests are the first check.
"""
import ctypes
import json
import os
import re
import signal
import subprocess
import sys
import time

import pytest

from app import supervisor
from app.model import ModelManager
from app.settings import Settings

from .kit import DESKTOP, TESTS, fake_python
from .test_app_http import http, wait_for

SUPERVISOR = str(DESKTOP / "app" / "supervisor.py")
POSIX = sys.platform != "win32"


def gone(pids, timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not any(supervisor.alive(pid) for pid in pids):
            return True
        time.sleep(0.1)
    return False


def read_json(path, timeout=20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with open(path, encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, ValueError):
            time.sleep(0.1)
    raise AssertionError(f"{path} was not written")


def hard_kill(pid):
    """What `kill -9` and Task Manager's "End task" do: no handler runs in the target."""
    if POSIX:
        os.kill(pid, signal.SIGKILL)
    else:
        subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)


@pytest.fixture
def leftovers():
    """Pids a test started; whatever is still running afterwards is killed (and fails nothing)."""
    pids: list[int] = []
    yield pids
    for pid in pids:
        if supervisor.alive(pid):
            if POSIX:
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
            else:
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)


def same_server(recorded: int, reported: int) -> bool:
    """Is the server pid the supervisor recorded the one the fake server reported?
    On Windows the stand-in interpreter is a .cmd file, so the recorded process is the
    cmd.exe that runs it and the fake server is its child: only "a live process, not
    the fake server itself" can be checked there."""
    if sys.platform == "win32":
        return recorded != reported and supervisor.alive(recorded)
    return recorded == reported


def start_supervised(tmp_path, leftovers, *options, stubborn=False):
    pids_file, pid_file = tmp_path / "pids.json", tmp_path / "run" / "server-test.json"
    env = dict(os.environ, FAKE_SERVE_PIDS=str(pids_file), FAKE_SERVE_STUBBORN="1" if stubborn else "0")
    process = subprocess.Popen(
        [sys.executable, SUPERVISOR, "--owner-pid", str(os.getpid()), "--pid-file", str(pid_file), *options, "--",
         sys.executable, str(TESTS / "fake_serve.py"), "-m", "stepserver.cli", "serve", "--port", "0"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
    leftovers.append(process.pid)
    pids = read_json(pids_file)
    leftovers.extend([pids["server"], pids["worker"]])
    return process, pids, pid_file


def test_closing_the_control_pipe_stops_server_and_workers(tmp_path, leftovers):
    process, pids, pid_file = start_supervised(tmp_path, leftovers)
    record = read_json(pid_file)
    assert record["app_pid"] == os.getpid() and record["supervisor_pid"] == process.pid
    assert record["server_start"] == supervisor.start_id(record["server_pid"])
    assert supervisor.is_recorded_process(record["server_pid"], record["server_start"], record["server_command"])
    assert supervisor.alive(pids["server"]) and supervisor.alive(pids["worker"])
    process.stdin.close()                                   # all the app does to stop its server
    output_wanted = ["the app closed the control pipe"]
    if sys.platform == "win32":
        assert process.wait(15) == 1                        # Windows has no Ctrl+C for a child: it is terminated
    else:
        assert process.wait(15) == 0                        # the fake server ends on Ctrl+C, like the step server
        output_wanted.append("interrupted")
    output = process.stdout.read().decode()
    assert all(text in output for text in output_wanted)
    assert gone([pids["server"], pids["worker"]])           # the worker ignores every signal but KILL
    assert not pid_file.exists()


def test_a_server_that_ignores_the_request_is_killed_after_the_grace_period(tmp_path, leftovers):
    process, pids, pid_file = start_supervised(tmp_path, leftovers, "--grace", "0.5", stubborn=True)
    started = time.time()
    process.stdin.close()
    process.wait(15)
    assert time.time() - started < 10
    assert gone([pids["server"], pids["worker"]]) and not pid_file.exists()


def test_the_supervisor_ends_when_the_server_does(tmp_path, leftovers):
    process, pids, pid_file = start_supervised(tmp_path, leftovers)
    hard_kill(pids["server"])
    assert process.wait(15) != 0                            # the app sees the server's failure
    assert gone([pids["worker"]]) and not pid_file.exists()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="PR_SET_PDEATHSIG is Linux only")
def test_linux_a_killed_supervisor_takes_the_server_down(tmp_path, leftovers):
    process, pids, _pid_file = start_supervised(tmp_path, leftovers)
    hard_kill(process.pid)
    process.wait(10)
    assert gone([pids["server"]])                           # the real engine workers then lose their stdin and exit


@pytest.fixture
def app_with_fake_server(fusion, tmp_path, leftovers):
    """The real app, headless, starting its model server itself (a fake interpreter runs fake_serve.py)."""
    config, pids_file = tmp_path / "config", tmp_path / "pids.json"
    config.mkdir()
    (config / "settings.json").write_text(json.dumps({
        "log_dir": str(tmp_path / "history"), "save_history": True,
        "model": {"mode": "spawn", "weights": "base", "python": fake_python(tmp_path / "bin")}}), encoding="utf-8")
    process = subprocess.Popen(
        [sys.executable, str(DESKTOP / "fusion_needle.py"), "--headless", "--config-dir", str(config),
         "--fusion-url", fusion.url],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=dict(os.environ, FAKE_SERVE_PIDS=str(pids_file)))
    leftovers.append(process.pid)
    try:
        url = re.search(r"http://127\.0\.0\.1:\d+/", process.stdout.readline()).group(0)
        token = re.search(r'name="fn-token" content="([^"]+)"', http(url)[2].decode()).group(1)

        def state():
            return json.loads(http(url + "api/state", token=token)[2])

        ready = wait_for(lambda: (lambda s: s if s["model"]["state"] == "ready" else None)(state()))
        pids = read_json(pids_file)
        leftovers.extend([ready["model"]["pid"], pids["server"], pids["worker"]])
        yield {"process": process, "url": url, "token": token, "state": state, "ready": ready, "pids": pids,
               "run": config / "run"}
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(10)


def test_hard_kill_of_the_app_takes_the_server_and_workers_with_it(app_with_fake_server):
    running = app_with_fake_server
    model, pids = running["ready"]["model"], running["pids"]
    assert model["managed"] and model["model"] == "fake-serve.cact"
    assert model["pid"] != pids["server"]                                               # pid: the supervisor
    assert same_server(model["server_pid"], pids["server"])
    assert pids["token"] and "stepserver.cli" in pids["argv"]      # started the way the step server is started
    everything = [model["pid"], pids["server"], pids["worker"]]
    assert all(supervisor.alive(pid) for pid in everything)
    assert len(list(running["run"].glob("server-*.json"))) == 1
    hard_kill(running["process"].pid)                       # no atexit, no signal handler, no finally
    running["process"].wait(10)
    assert gone(everything), "the model server or its worker outlived the app"
    assert not list(running["run"].glob("server-*.json"))


def test_quit_stops_the_server_and_removes_the_pid_file(app_with_fake_server):
    running = app_with_fake_server
    everything = [running["ready"]["model"]["pid"], running["pids"]["server"], running["pids"]["worker"]]
    assert http(running["url"] + "api/quit", body={}, token=running["token"])[0] == 200
    assert running["process"].wait(30) == 0
    assert gone(everything, 5) and not list(running["run"].glob("server-*.json"))


# ---- a server left behind by a crashed launch ---------------------------------------

def orphan_server(tmp_path, leftovers):
    """A fake server running without app and supervisor, as after both were killed at once."""
    pids_file = tmp_path / "orphan-pids.json"
    command = [sys.executable, str(TESTS / "fake_serve.py"), "-m", "stepserver.cli", "serve", "--port", "0"]
    kwargs = {"start_new_session": True} if POSIX else {}
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               env=dict(os.environ, FAKE_SERVE_PIDS=str(pids_file)), **kwargs)
    pids = read_json(pids_file)
    leftovers.extend([process.pid, pids["worker"]])
    return process, pids, command


def dead_pid():
    finished = subprocess.Popen([sys.executable, "-c", "pass"])
    finished.wait()
    return finished.pid


def write_record(run, name, **fields):
    run.mkdir(parents=True, exist_ok=True)
    (run / name).write_text(json.dumps(fields), encoding="utf-8")
    return run / name


def test_stale_server_of_a_dead_app_is_stopped_at_the_next_start(tmp_path, leftovers, monkeypatch):
    process, pids, command = orphan_server(tmp_path, leftovers)
    monkeypatch.setenv("FUSION_NEEDLE_CONFIG_DIR", str(tmp_path / "config"))
    run = supervisor.run_dir(tmp_path / "config")
    record = write_record(run, "server-1-aa.json", app_pid=dead_pid(), app_start="1",
                          supervisor_pid=dead_pid(), supervisor_start="1", supervisor_command=["x", "y"],
                          server_pid=process.pid, server_start=supervisor.start_id(process.pid),
                          server_command=command)
    (run / "server-2-bb.json").write_text("not json", encoding="utf-8")
    manager = ModelManager(Settings(tmp_path / "config" / "settings.json"))
    manager.cleanup_stale()                                 # what every start does first
    process.wait(10)
    assert gone([process.pid, pids["worker"]])
    assert not record.exists() and not list(run.glob("*.json"))
    assert any("leftover model server" in line for line in manager.log)


def test_stale_cleanup_never_kills_what_it_cannot_prove_is_ours(tmp_path, leftovers):
    process, pids, command = orphan_server(tmp_path, leftovers)
    run = tmp_path / "run"
    start = supervisor.start_id(process.pid)
    # the pid is in use again by another process (other start time), or by another command
    reused = write_record(run, "server-1-aa.json", app_pid=dead_pid(), app_start="1", server_pid=process.pid,
                          server_start=str(int(start) + 1) if start.isdigit() else start + "x",
                          server_command=command)
    other = write_record(run, "server-1-bb.json", app_pid=dead_pid(), app_start="1", server_pid=process.pid,
                         server_start=start, server_command=["python", "-m", "something.else", "--port", "1"])
    notes = []
    done = supervisor.cleanup_stale(run, notes.append)
    assert [entry["killed"] for entry in done] == [[], []]
    assert all(entry["skipped"][0]["pid"] == process.pid for entry in done) and len(notes) == 2
    assert supervisor.alive(process.pid) and supervisor.alive(pids["worker"])
    assert not reused.exists() and not other.exists()       # the files are stale either way


def test_stale_cleanup_leaves_the_server_of_a_running_app_alone(tmp_path, leftovers):
    process, pids, command = orphan_server(tmp_path, leftovers)
    other_app = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    leftovers.append(other_app.pid)
    run = tmp_path / "run"
    record = write_record(run, "server-1-aa.json", app_pid=other_app.pid,
                          app_start=supervisor.start_id(other_app.pid), server_pid=process.pid,
                          server_start=supervisor.start_id(process.pid), server_command=command)
    assert supervisor.cleanup_stale(run) == []
    assert record.exists() and supervisor.alive(process.pid)
    other_app.kill()
    other_app.wait(10)
    assert len(supervisor.cleanup_stale(run)[0]["killed"]) == 1          # now it is stale
    process.wait(10)
    assert gone([process.pid, pids["worker"]])


def test_start_id_tells_processes_apart():
    assert supervisor.start_id(os.getpid()) and supervisor.alive(os.getpid(), supervisor.start_id(os.getpid()))
    assert not supervisor.alive(os.getpid(), "not-this-one")
    assert supervisor.start_id(dead_pid()) is None and supervisor.start_id(0) is None
    assert supervisor.start_id(True) is None and supervisor.start_id(-5) is None


def test_model_manager_starts_through_the_supervisor_and_stops_everything(tmp_path, leftovers, monkeypatch):
    monkeypatch.setenv("FUSION_NEEDLE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("FAKE_SERVE_PIDS", str(tmp_path / "pids.json"))
    settings = Settings(tmp_path / "config" / "settings.json")
    settings.update({"model": {"mode": "spawn", "weights": "base", "python": fake_python(tmp_path / "bin")}})
    manager = ModelManager(settings)
    try:
        manager.start_async().join(60)
        assert manager.state == "ready", manager.detail
        pids = read_json(tmp_path / "pids.json")
        leftovers.extend([manager.process.pid, pids["server"], pids["worker"]])
        snapshot = manager.snapshot()
        assert snapshot["pid"] == manager.process.pid and same_server(snapshot["server_pid"], pids["server"])
        assert manager.pid_file.is_file() and manager.token and manager.token not in " ".join(manager.log)
        everything = [manager.process.pid, pids["server"], pids["worker"]]
    finally:
        manager.stop()
    assert gone(everything, 5) and not manager.pid_file.exists()


def test_windows_job_structures_have_the_documented_layout():
    """Checked here because a wrong size makes SetInformationJobObject fail on Windows (64-bit layout)."""
    if ctypes.sizeof(ctypes.c_void_p) != 8:
        pytest.skip("layout checked for 64-bit only")
    assert ctypes.sizeof(supervisor._BasicLimits) == 64
    assert ctypes.sizeof(supervisor._IoCounters) == 48
    assert ctypes.sizeof(supervisor._ExtendedLimits) == 144
    assert supervisor._ExtendedLimits.BasicLimitInformation.offset == 0
    assert supervisor._BasicLimits.LimitFlags.offset == 16
    assert supervisor.bind_to_this_process(None) == "" or sys.platform == "win32"
