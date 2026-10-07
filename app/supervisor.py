"""Keep the model server's lifetime tied to the app's, whatever kills the app.

The app does not start the step server directly. It starts this file, which starts
the server and stops it (and the engine workers under it) as soon as the app is
gone:

    app ──stdin pipe──▶ supervisor ──▶ the step server ──▶ engine workers

* every OS: the app holds the write end of the supervisor's stdin. When the app
  ends for any reason (Quit, a crash, SIGKILL, Task Manager) the pipe closes;
  the supervisor reads EOF and stops the server. The supervisor also watches
  the owner's process directly, in case stdin is not usable.
* POSIX: the server runs in its own session, so the supervisor can signal the
  whole group (server + workers). Linux additionally sets PR_SET_PDEATHSIG on
  the server, which covers a supervisor that is itself killed hard. (It is not
  set on the supervisor: the signal is tied to the *thread* that started the
  child, and the app starts it from a short-lived thread.)
* Windows: the supervisor puts itself into a Job Object with
  KILL_ON_JOB_CLOSE before it starts the server, so the kernel kills the server
  and its workers when the supervisor ends; the app does the same with the
  supervisor (`bind_to_this_process`). NOT TESTED: written on Linux.

A pid file per launch (``<config dir>/run/server-*.json``) records the app, the
supervisor and the server with their start times. `cleanup_stale` reads those
files at the next start and stops what a crashed launch left behind, but only
processes it can prove are the recorded ones (same pid *and* same start time,
and the same command line where the OS lets us read it).

Standard library only, no imports from the app: this file is run by path
(``python supervisor.py ...``) in a checkout and as ``-m app.supervisor`` in a
frozen build.
"""
from __future__ import annotations

import argparse
import contextlib
import ctypes
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

WINDOWS = sys.platform == "win32"
LINUX = sys.platform.startswith("linux")
GRACE = 8.0                     # seconds the server gets to close its engine after the first signal


# ---- process identity ---------------------------------------------------------

def _linux_stat(pid: int) -> tuple[str, str] | None:
    """(state, starttime) from /proc/<pid>/stat."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            text = handle.read().decode("ascii", "replace")
    except OSError:
        return None
    fields = text[text.rfind(")") + 2:].split()         # the command name may hold spaces and brackets
    if len(fields) < 20:
        return None
    return fields[0], fields[19]


def start_id(pid: int) -> str | None:
    """A value that identifies this very process (not a later one with the same pid); None when gone."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    if WINDOWS:
        return _win_start_id(pid)
    if LINUX:
        found = _linux_stat(pid)
        if found is None or found[0] in ("Z", "X"):     # a zombie is not running any more
            return None
        return found[1]
    try:                                                # macOS, BSD
        out = subprocess.run(["ps", "-o", "stat=", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True,
                             timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    text = out.stdout.strip()
    if out.returncode != 0 or not text or text.startswith("Z"):
        return None
    return " ".join(text.split()[1:]) or None


def alive(pid: int, expected_start: str | None = None) -> bool:
    found = start_id(pid)
    return found is not None and (expected_start is None or found == expected_start)


def command_line(pid: int) -> list[str] | None:
    """argv of a running process, where the OS gives it to us without extra tools (Linux); else None."""
    if LINUX:
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as handle:
                raw = handle.read()
        except OSError:
            return None
        return [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]
    if WINDOWS:
        return None
    try:
        out = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.split() if out.returncode == 0 and out.stdout.strip() else None


def is_recorded_process(pid, start, command) -> bool:
    """Is the process with this pid the one a pid file recorded? Unsure means no."""
    if not isinstance(pid, int) or not isinstance(start, str) or not start:
        return False
    if start_id(pid) != start:
        return False
    wanted = [str(part) for part in command] if isinstance(command, list) else []
    if WINDOWS:
        image = _win_image(pid)
        if image is None or not wanted:
            return False
        return os.path.basename(image).lower() == os.path.basename(wanted[0]).lower()
    current = command_line(pid)
    if current is None:
        return True                                     # pid + start time is all this OS offers
    if len(wanted) < 2:
        return False
    if LINUX:
        return all(part in current for part in wanted[1:])
    joined = " ".join(current)                          # `ps` output: words, not argv
    return all(word in joined for part in wanted[1:] for word in part.split())


# ---- stopping -------------------------------------------------------------------

def _wait_gone(pids: list[int], timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not any(alive(pid) for pid in pids):
            return True
        time.sleep(0.1)
    return not any(alive(pid) for pid in pids)


def kill_tree(pid: int, wait: float = 3.0):
    """Hard-stop a process and what it started: its process group (POSIX), its tree (Windows)."""
    if WINDOWS:
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=30,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return
    try:
        if os.getpgid(pid) == pid and pid != os.getpgrp():
            os.killpg(pid, signal.SIGKILL)              # a leader we started with start_new_session: all of it
        else:
            os.kill(pid, signal.SIGKILL)
    except OSError:
        return
    _wait_gone([pid], wait)


# ---- pid files ------------------------------------------------------------------

def run_dir(config_dir) -> Path:
    return Path(config_dir) / "run"


def _write_json(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump(data, handle)
    os.replace(temp, path)


def cleanup_stale(directory, note=None) -> list[dict]:
    """Stop servers left behind by launches whose app is gone. -> what was done, per pid file.

    A file whose app is still running belongs to another open window and is
    left alone. A recorded process is only stopped when it is provably the
    recorded one; otherwise the file is just removed.
    """
    done = []
    directory = Path(directory)
    try:
        files = sorted(directory.glob("server-*.json"))
    except OSError:
        return done
    for path in files:
        try:
            with open(path, encoding="utf-8") as handle:
                record = json.load(handle)
            if not isinstance(record, dict):
                raise ValueError("not an object")
        except (OSError, ValueError):
            with contextlib.suppress(OSError):
                path.unlink()
            continue
        if record.get("app_pid") == os.getpid() or alive(record.get("app_pid"), record.get("app_start") or "?"):
            continue
        entry = {"file": path.name, "killed": [], "skipped": []}
        for role in ("supervisor", "server"):           # the supervisor first, so it cannot react
            pid, start = record.get(role + "_pid"), record.get(role + "_start")
            if not alive(pid):
                continue
            if is_recorded_process(pid, start, record.get(role + "_command")):
                kill_tree(pid)
                entry["killed"].append({"role": role, "pid": pid})
            else:
                entry["skipped"].append({"role": role, "pid": pid, "why": "not provably the recorded process"})
        with contextlib.suppress(OSError):
            path.unlink()
        done.append(entry)
        if note is not None:
            for item in entry["killed"]:
                note(f"stopped a leftover model {item['role']} (pid {item['pid']}) from an earlier launch")
            for item in entry["skipped"]:
                note(f"pid {item['pid']} from an earlier launch left alone: {item['why']}")
    return done


# ---- Windows: Job Objects (NOT TESTED) --------------------------------------------

JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SYNCHRONIZE = 0x00100000
STILL_ACTIVE = 259
WAIT_OBJECT_0 = 0


class _BasicLimits(ctypes.Structure):                   # JOBOBJECT_BASIC_LIMIT_INFORMATION
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", ctypes.c_uint32), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", ctypes.c_uint32),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", ctypes.c_uint32),
                ("SchedulingClass", ctypes.c_uint32)]


class _IoCounters(ctypes.Structure):                    # IO_COUNTERS
    _fields_ = [(name, ctypes.c_uint64) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class _ExtendedLimits(ctypes.Structure):                # JOBOBJECT_EXTENDED_LIMIT_INFORMATION
    _fields_ = [("BasicLimitInformation", _BasicLimits), ("IoInfo", _IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]


class _FileTime(ctypes.Structure):
    _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]


_kernel32 = None
_jobs: list = []                                        # job handles, kept open for the life of this process


def _k32():
    global _kernel32
    if _kernel32 is None:
        handle, dword, pointer = ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p
        lib = ctypes.WinDLL("kernel32", use_last_error=True)
        for name, result, arguments in (
                ("CreateJobObjectW", handle, [pointer, ctypes.c_wchar_p]),
                ("SetInformationJobObject", ctypes.c_int, [handle, ctypes.c_int, pointer, dword]),
                ("AssignProcessToJobObject", ctypes.c_int, [handle, handle]),
                ("GetCurrentProcess", handle, []),
                ("OpenProcess", handle, [dword, ctypes.c_int, dword]),
                ("CloseHandle", ctypes.c_int, [handle]),
                ("GetProcessTimes", ctypes.c_int, [handle, pointer, pointer, pointer, pointer]),
                ("GetExitCodeProcess", ctypes.c_int, [handle, pointer]),
                ("QueryFullProcessImageNameW", ctypes.c_int, [handle, dword, ctypes.c_wchar_p, pointer]),
                ("WaitForSingleObject", dword, [handle, dword])):
            function = getattr(lib, name)
            function.restype, function.argtypes = result, arguments
        _kernel32 = lib
    return _kernel32


def _win_open(pid: int, access: int = PROCESS_QUERY_LIMITED_INFORMATION):
    return _k32().OpenProcess(access, 0, pid) or None


def _win_start_id(pid: int) -> str | None:
    handle = _win_open(pid)
    if handle is None:
        return None
    try:
        code = ctypes.c_uint32(0)
        if not _k32().GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != STILL_ACTIVE:
            return None
        times = [_FileTime() for _ in range(4)]
        if not _k32().GetProcessTimes(handle, *[ctypes.byref(item) for item in times]):
            return None
        return str((times[0].high << 32) | times[0].low)
    finally:
        _k32().CloseHandle(handle)


def _win_image(pid: int) -> str | None:
    handle = _win_open(pid)
    if handle is None:
        return None
    try:
        size = ctypes.c_uint32(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not _k32().QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return None
        return buffer.value
    finally:
        _k32().CloseHandle(handle)


def _kill_on_close_job():
    """A Job Object whose processes the kernel kills when its last handle closes (this process ends)."""
    job = _k32().CreateJobObjectW(None, None)
    if not job:
        raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")
    info = _ExtendedLimits()
    info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not _k32().SetInformationJobObject(job, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
                                          ctypes.byref(info), ctypes.sizeof(info)):
        error = ctypes.get_last_error()
        _k32().CloseHandle(job)
        raise OSError(error, "SetInformationJobObject failed")
    _jobs.append(job)                                   # never closed on purpose
    return job


def bind_to_this_process(process: subprocess.Popen) -> str:
    """Windows: make ``process`` die with this process. -> '' or why it could not be done.

    The handle of the job stays open in this process only (it is not
    inheritable), so the kernel closes it when this process ends, however that
    happens, and then kills what is in the job.
    """
    if not WINDOWS:
        return ""
    try:
        job = _jobs[0] if _jobs else _kill_on_close_job()
        if not _k32().AssignProcessToJobObject(job, int(process._handle)):       # noqa: SLF001
            raise OSError(ctypes.get_last_error(), "AssignProcessToJobObject failed")
    except Exception as failure:                        # the stdin pipe still does the job
        return f"job object not used: {failure}"
    return ""


def _enter_own_job() -> str:
    """Windows: put *this* process in a kill-on-close job, so every child it starts is in it from birth."""
    try:
        job = _kill_on_close_job()
        if not _k32().AssignProcessToJobObject(job, _k32().GetCurrentProcess()):
            raise OSError(ctypes.get_last_error(), "AssignProcessToJobObject failed")
    except Exception as failure:
        return f"job object not used: {failure}"
    return ""


# ---- the supervisor process -------------------------------------------------------

def _set_pdeathsig():                                   # runs in the child, between fork and exec
    with contextlib.suppress(Exception):
        _LIBC.prctl(1, int(signal.SIGTERM), 0, 0, 0)    # PR_SET_PDEATHSIG


_LIBC = None


class Supervisor:
    def __init__(self, command: list[str], owner_pid: int, pid_file: str | None, grace: float = GRACE):
        self.command = command
        self.owner_pid = owner_pid
        self.owner_start = start_id(owner_pid)
        self.pid_file = Path(pid_file) if pid_file else None
        self.grace = grace
        self.stop = threading.Event()
        self.why = ""
        self.child: subprocess.Popen | None = None
        self.direct_parent = not WINDOWS and os.getppid() == owner_pid
        self.owner_handle = None
        self._last_owner_check = 0.0

    def say(self, text: str):
        with contextlib.suppress(Exception):
            print(f"[supervisor] {text}", flush=True)

    def request_stop(self, why: str):
        if not self.stop.is_set():
            self.why = why
            self.stop.set()

    # -- watching the owner --
    def _watch_stdin(self):
        try:
            while True:
                data = os.read(0, 4096)
                if not data:
                    self.request_stop("the app closed the control pipe")
                    return
                if b"stop" in data:
                    self.request_stop("the app asked for a stop")
                    return
        except OSError as failure:                      # no usable stdin: the owner watch still works
            self.say(f"control pipe not readable ({failure}); watching the app process instead")

    def _owner_gone(self) -> bool:
        if WINDOWS:
            if self.owner_handle is None:
                return False
            return _k32().WaitForSingleObject(self.owner_handle, 0) == WAIT_OBJECT_0
        if self.direct_parent:
            return os.getppid() != self.owner_pid
        now = time.time()
        if now - self._last_owner_check < (1.0 if LINUX else 5.0):
            return False
        self._last_owner_check = now
        return not alive(self.owner_pid, self.owner_start)

    # -- the server --
    def _spawn(self):
        kwargs: dict = {"stdin": subprocess.DEVNULL}
        if WINDOWS:
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        else:
            kwargs["start_new_session"] = True          # its own group: signals reach the workers too
            if LINUX:
                global _LIBC
                with contextlib.suppress(Exception):
                    _LIBC = ctypes.CDLL(None, use_errno=True)
                    _LIBC.prctl                          # resolve before fork
                    kwargs["preexec_fn"] = _set_pdeathsig
        self.child = subprocess.Popen(self.command, **kwargs)

    def _record(self):
        if self.pid_file is None:
            return
        child = self.child
        try:
            _write_json(self.pid_file, {
                "app_pid": self.owner_pid, "app_start": self.owner_start,
                "supervisor_pid": os.getpid(), "supervisor_start": start_id(os.getpid()),
                "supervisor_command": [sys.executable] + sys.argv[1:],
                "server_pid": child.pid, "server_start": start_id(child.pid),
                "server_command": self.command, "started": time.time()})
        except OSError as failure:
            self.say(f"pid file not written ({failure})")

    def _exited(self) -> bool:
        """Has the server ended? On POSIX it is left unreaped, so its pid (= its group id) stays ours."""
        child = self.child
        if child.returncode is not None:
            return True
        if not WINDOWS and hasattr(os, "waitid"):
            try:
                return os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None
            except ChildProcessError:
                return True
            except OSError:
                pass
        return child.poll() is not None

    def _signal_group(self, number: int):
        with contextlib.suppress(OSError):
            os.killpg(self.child.pid, number)

    def _wait_exit(self, seconds: float) -> bool:
        deadline = time.time() + seconds
        while time.time() < deadline:
            if self._exited():
                return True
            time.sleep(0.05)
        return self._exited()

    def _shutdown(self):
        """Ask nicely (the server closes its engine on Ctrl+C), then take the whole group down."""
        child = self.child
        if WINDOWS:
            if child.poll() is None:
                with contextlib.suppress(OSError):
                    child.terminate()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    child.wait(timeout=5)
            if child.poll() is None or not _jobs:
                kill_tree(child.pid)                    # no job: stop the tree by hand
            return                                      # with a job, the kernel kills the rest when we end
        if not self._exited():
            with contextlib.suppress(OSError):
                os.kill(child.pid, signal.SIGINT)
            if not self._wait_exit(self.grace):
                self._signal_group(signal.SIGTERM)
                self._wait_exit(2.0)
        self._signal_group(signal.SIGKILL)              # the leader is a zombie at most: the group id is still ours
        with contextlib.suppress(Exception):
            child.wait(timeout=5)

    def run(self) -> int:
        if self.owner_start is None:
            self.say("the app is gone already; not starting the model server")
            return 0
        if WINDOWS:
            problem = _enter_own_job()
            if problem:
                self.say(problem)
            self.owner_handle = _win_open(self.owner_pid, SYNCHRONIZE)
        try:
            self._spawn()
        except OSError as failure:
            self.say(f"could not start {self.command[0]}: {failure}")
            return 127
        self._record()
        for name in ("SIGTERM", "SIGINT", "SIGHUP", "SIGBREAK"):
            if hasattr(signal, name):
                with contextlib.suppress(ValueError, OSError):
                    signal.signal(getattr(signal, name), lambda number, _frame: self.request_stop(f"signal {number}"))
        threading.Thread(target=self._watch_stdin, name="control-pipe", daemon=True).start()
        try:
            while not self._exited():
                if self.stop.wait(0.2):
                    break
                if self._owner_gone():
                    self.request_stop("the app is gone")
                    break
        finally:
            if self.stop.is_set():
                self.say(f"stopping the model server: {self.why}")
            self._shutdown()
            if self.pid_file is not None:
                with contextlib.suppress(OSError):
                    self.pid_file.unlink()
        code = self.child.returncode
        if code is None:
            return 1
        return code if code >= 0 else 128 - code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fusion-needle-supervisor", description=__doc__.split("\n")[0])
    parser.add_argument("--owner-pid", type=int, default=0, help="the app's pid (default: the parent)")
    parser.add_argument("--pid-file", default="")
    parser.add_argument("--grace", type=float, default=GRACE)
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--" not in argv:                                # split by hand: the command has options of its own
        parser.error("usage: supervisor.py [options] -- <server command>")
    cut = argv.index("--")
    args, command = parser.parse_args(argv[:cut]), argv[cut + 1:]
    if not command:
        parser.error("no command given")
    return Supervisor(command, args.owner_pid or os.getppid(), args.pid_file or None, args.grace).run()


if __name__ == "__main__":
    sys.exit(main())
