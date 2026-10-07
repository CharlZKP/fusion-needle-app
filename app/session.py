"""The loop between the model and Fusion (README.md).

    features = split(goal)
    for feature in features:
        state  = parse_state(read-only state script)
        answer = POST /v1/step {query: feature, system: state, tools: names?}
        empty answer      -> stop, show reasoning + suppressed, ask the user
        guard (guard.py)  -> a call the request does not back, the schema forbids or the
                             state cannot take is not run: show the call and the reason
        for call in answer.calls:
            open dialog   -> stop, ask the user to close it
            failure       -> undo once per call already done, stop, show the error
        record the step in the session history (when it is switched on)

One worker thread runs at a time: the model server answers one request at a
time and Fusion executes one script at a time.

A row is written when the user moves on (next step, new goal, quit). Until then
it is "pending", so "Undo last step" can withdraw a step without ever rewriting
the log file: `answers` is what was executed *and accepted*.
"""
from __future__ import annotations

import copy
import threading
import time

from . import phrases
from .diagnostics import raw_text
from .goal import split_goal
from .guard import EVIDENCE, RULE_CODES, Guard
from .history import NO_CALL_REASONS, category_of, offered_schemas
from .mcp import McpError

MAX_CALLS = 4
MAX_IMAGES = 48


class ActionError(Exception):
    """The request cannot be done; the message is for the user."""


class Busy(ActionError):
    pass


class Step:
    def __init__(self, index: int, text: str):
        self.index = index
        self.text = text
        self.locked = False           # the active document changed during or after this step: no undo
        self.reset()

    def reset(self):
        self.status = "pending"       # pending reading asking proposed empty running dialog choice
        #                               failed error done negative skipped
        self.state_line = ""
        self.state_after = ""
        self.unhealthy: list = []
        self.sent_tools: list[str] | None = None      # names sent; None = `tools` omitted
        self.answer: dict | None = None
        self.calls: list[dict] = []                   # the calls to run (the user may have edited them)
        self.corrected = False
        self.confirm: list[str] = []
        self.blocked = False                          # the guard holds the calls back (see ``guard``)
        self.guard: dict | None = None                # the guard's verdict on the calls (guard.Guard.check)
        self.help: dict | None = None                 # what to tell the user when nothing ran (phrases.refusal)
        self.results: list[dict] = []
        self.payloads: list[dict] = []
        self.next_call = 0
        self.undo_count = 0                           # executed calls that one Fusion undo each takes back
        self.undone = 0
        self.error = ""
        self.error_call: int | None = None
        self.dialog = ""
        self.dialog_unknown = False                   # the dialog check's answer could not be read
        self.dialog_raw = ""                          # ... and this is what Fusion sent (base64 removed)
        self.dialog_override = False                  # the user said "proceed anyway" for this step
        self.choice: list[dict] = []                  # documents to pick from (open_document)
        self.chosen: str | None = None
        self.notes: list[str] = []
        self.row_status = ""                          # "" pending logged not_logged withdrawn
        self.row_note = ""
        self.row_line: int | None = None
        self.row_id = ""
        self.model_ms: float | None = None

    def to_dict(self) -> dict:
        answer = self.answer or {}
        return {
            "index": self.index, "text": self.text, "status": self.status,
            "state": self.state_line, "state_after": self.state_after, "unhealthy": self.unhealthy,
            "tools": self.sent_tools, "tools_mode": "omitted" if self.sent_tools is None else "names",
            "model_calls": answer.get("calls", []), "suppressed": answer.get("suppressed", []),
            "reasoning": answer.get("reasoning", ""), "engine_error": answer.get("engine_error", ""),
            "retrieval": answer.get("retrieval"), "agent_cache": answer.get("agent_cache"),
            "latency_ms": answer.get("latency_ms"), "model_ms": self.model_ms,
            "answered": self.answer is not None,
            "calls": self.calls, "corrected": self.corrected, "confirm": self.confirm, "blocked": self.blocked,
            "results": self.results, "next_call": self.next_call, "undone": self.undone,
            "error": self.error, "error_call": self.error_call, "dialog": self.dialog,
            "dialog_unknown": self.dialog_unknown, "dialog_raw": self.dialog_raw,
            "dialog_override": self.dialog_override, "choice": self.choice, "notes": self.notes,
            "row_status": self.row_status, "row_note": self.row_note, "row_line": self.row_line,
            "row_id": self.row_id, "guard": self.guard, "help": self.help,
        }


class Session:
    def __init__(self, *, fusion, model, backend, log, settings: dict, validator=None,
                 export_dir: str | None = None, synchronous: bool = False, diagnostics=None,
                 guard: Guard | None = None, examples: phrases.Examples | None = None):
        """
        fusion    mcp.FusionClient (``call(payload) -> parsed result``)
        model     ``step(query, system, names_or_None) -> dict``, ``catalogue() -> list``,
                  ``max_tools() -> int``, ``name() -> str``
        backend   backend.Backend
        log       history.SessionLog
        validator unused in this build (None)
        diagnostics diagnostics.Diagnostics or None
        guard     guard.Guard (default: one without project rule files: schema and numbers only)
        examples  phrases.Examples, for the hints of a step that was not run
        """
        self.diagnostics = diagnostics
        self.guard = guard or Guard()
        self.examples = examples or phrases.Examples()
        self.guard_counts = {"pass": 0, "blocked": 0, "no_call": 0, "overridden": 0, "edited": 0}
        self.fusion = fusion
        self.model = model
        self.backend = backend
        self.log = log
        self.settings = settings
        self.validator = validator
        self.export_dir = export_dir
        self.synchronous = synchronous
        self.goal = ""
        self.goal_id = ""
        self.steps: list[Step] = []
        self.auto = False
        self.activity = ""
        self.message = ""                     # last notice for the user (not tied to a step)
        self.fusion_state = ""
        self.fusion_unhealthy: list = []
        self.fusion_state_error = ""
        self.images: dict[str, dict] = {}
        self._image_seq = 0
        self._row_seq = 0
        self._pending: tuple[Step, dict] | None = None
        self._busy = False
        self._stop = False
        self._worker: threading.Thread | None = None
        self._lock = threading.RLock()
        self.version = 0

    # ---- plumbing ------------------------------------------------------
    def _touch(self):
        self.version += 1

    def _set(self, step: Step | None = None, status: str | None = None, activity: str | None = None):
        if step is not None and status is not None:
            step.status = status
        if activity is not None:
            self.activity = activity
        self._touch()

    @property
    def busy(self) -> bool:
        return self._busy

    def _submit(self, function, *args):
        with self._lock:
            if self._busy:
                raise Busy("still working on the previous action")
            self._busy = True
            self._stop = False
            self.message = ""

        def run():
            try:
                function(*args)
            except Exception as failure:               # never leave the session locked
                self.message = f"internal error: {type(failure).__name__}: {failure}"
            finally:
                self.activity = ""
                self._busy = False
                self._touch()

        if self.synchronous:
            run()
            return
        self._worker = threading.Thread(target=run, name="session-worker", daemon=True)
        self._worker.start()

    def wait(self, timeout: float | None = None) -> bool:
        worker = self._worker
        if worker is not None:
            worker.join(timeout)
            return not worker.is_alive()
        return True

    def _step(self, index) -> Step:
        if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(self.steps):
            raise ActionError("no such step")
        return self.steps[index]

    def _require_idle(self):
        if self._busy:
            raise Busy("still working on the previous action")

    # ---- queries -------------------------------------------------------
    def snapshot(self) -> dict:
        current = self._current_index()
        done = sum(1 for step in self.steps if step.status in ("done", "negative", "skipped"))
        return {
            "version": self.version, "goal": self.goal, "goal_id": self.goal_id, "busy": self._busy,
            "auto": self.auto, "activity": self.activity, "message": self.message, "current": current,
            "finished": bool(self.steps) and done == len(self.steps),
            "steps": [step.to_dict() for step in self.steps],
            "fusion_state": self.fusion_state, "fusion_unhealthy": self.fusion_unhealthy,
            "fusion_state_error": self.fusion_state_error,
            "can_undo_step": self._last_done() is not None, "guard_counts": dict(self.guard_counts),
            "log": {"file": str(self.log.path), "session": self.log.session, "rows": self.log.rows_written,
                    "pending": 1 if self._pending else 0},
        }

    def _current_index(self) -> int | None:
        for step in self.steps:
            if step.status not in ("done", "negative", "skipped"):
                return step.index
        return None

    def _last_done(self) -> Step | None:
        """The newest executed step, if nothing after it has touched the design."""
        for step in reversed(self.steps):
            if step.status == "done":
                return None if step.locked else step
            if step.status in ("negative", "skipped", "pending", "proposed", "empty", "error"):
                continue
            if step.status == "failed" and step.undo_count == 0:
                continue                               # its calls were taken back already
            return None                                # running / dialog: calls of a later step are in
        return None

    def script(self, index: int, call: int) -> dict:
        step = self._step(index)
        if not 0 <= call < len(step.payloads):
            raise ActionError("that call has not been rendered")
        payload = step.payloads[call]
        script = ((payload.get("arguments") or {}).get("object") or {}).get("script")
        return {"tool": payload.get("name"), "script": script,
                "arguments": None if script else payload.get("arguments")}

    # ---- actions (called from the UI) ----------------------------------
    def set_goal(self, goal: str):
        self._require_idle()
        features = split_goal(goal)
        if not features:
            raise ActionError("the goal is empty")
        self.commit_pending()
        self.goal = goal
        self.goal_id = self.log.new_part()
        self._row_seq = 0
        self.steps = [Step(index, text) for index, text in enumerate(features)]
        self.auto = False
        self.message = ""
        self._touch()

    def run(self, auto: bool):
        """Run-all (auto) or step-by-step: propose the next feature, execute when allowed."""
        if not self.steps:
            raise ActionError("write a goal first")
        current = self._current_index()
        if current is None:
            raise ActionError("every feature of this goal is done")
        step = self.steps[current]
        if step.status in ("empty", "dialog", "choice"):
            raise ActionError("this step is waiting for you")
        self.auto = bool(auto)
        if step.status in ("failed", "error"):
            self._submit(self._retry, step)
        elif step.status == "proposed" and auto and not step.confirm and not step.blocked:
            self._submit(self._execute_then_advance, step)
        elif step.status == "proposed":
            raise ActionError("this step is waiting for you to run, edit or skip it")
        else:
            self._submit(self._advance)

    def stop(self):
        """Pause after the call that is running now."""
        self._stop = True
        self.auto = False
        self._touch()

    def approve(self, index: int, calls: list | None = None, override: bool = False):
        """Run the proposed calls, or ``calls`` the user wrote instead.

        Calls the guard holds back run only with ``override``, and only when every
        finding is about evidence in the request (never a schema or state finding).
        Calls the user wrote are judged for schema and state; their values are the
        user's own, so missing evidence does not stop them (it keeps the row out of the log).
        """
        self._require_idle()
        step = self._step(index)
        if step.status not in ("proposed", "empty"):
            raise ActionError("this step is not waiting for approval")
        if step.index != self._current_index():
            raise ActionError("an earlier step is not finished")
        model_calls = (step.answer or {}).get("calls", [])
        if calls is None:
            if step.status == "empty":
                raise ActionError("there are no calls to run")
            calls = copy.deepcopy(step.calls)
        calls = self._check_calls(step, calls)
        if calls != step.calls:
            verdict = self._judge(step, calls)
            hard = [issue for issue in verdict["issues"] if issue["kind"] != EVIDENCE]
            if hard:
                raise ActionError(hard[0]["message"])
            if verdict["issues"]:
                verdict = {**verdict, "decision": "edited", "overridable": False}
                step.notes.append("You wrote these calls yourself. Not backed by the request text: "
                                  + " ".join(issue["message"] for issue in verdict["issues"]))
            step.guard = verdict
            self._guard_event(step, verdict["decision"], calls)
        elif step.blocked:
            verdict = step.guard or {}
            if not verdict.get("overridable"):
                reason = (verdict.get("issues") or [{}])[0].get("message", "a call does not match the catalogue")
                raise ActionError(f"not run: {reason} Rewrite the request, edit the calls or skip the step.")
            if not override:
                raise ActionError("this step was held back; choose Run anyway, rewrite the request or edit the calls")
            step.guard = {**verdict, "decision": "overridden"}
            step.notes.append("You chose Run anyway: the calls were sent although the request text does not "
                              "back every value.")
            self._guard_event(step, "overridden", calls)
        step.calls = calls
        step.corrected = calls != model_calls
        step.blocked = False
        self._submit(self._execute_then_advance, step)

    # ---- guard ---------------------------------------------------------
    def _judge(self, step: Step, calls: list) -> dict:
        """The guard's verdict. A guard that fails holds the calls back; it never lets them through unseen."""
        try:
            return self.guard.check(step.text, step.state_line, calls, self.model.catalogue(),
                                    offered=step.sent_tools, renderer_check=self.backend.validate)
        except Exception as failure:
            message = f"The check of this answer failed ({type(failure).__name__}: {failure}), so it was not run."
            return {"decision": "blocked", "overridable": True, "skipped": [],
                    "issues": [{"kind": EVIDENCE, "code": "guard_error", "call": 0, "tool": "", "message": message}]}

    def _refusal(self, step: Step, calls: list | None = None, verdict: dict | None = None) -> dict | None:
        try:
            return phrases.refusal(query=step.text, state_line=step.state_line, offered=step.sent_tools,
                                   catalogue=self.model.catalogue(), rules=self.guard.rules, examples=self.examples,
                                   calls=calls, verdict=verdict,
                                   engine_error=str((step.answer or {}).get("engine_error") or ""))
        except Exception:                               # a hint must never stop a step
            return None

    def _guard_event(self, step: Step, decision: str, calls: list | None = None):
        """Count the decision; everything but a plain pass also goes to the diagnostics events."""
        self.guard_counts[decision] = self.guard_counts.get(decision, 0) + 1
        if self.diagnostics is None or decision == "pass":
            return
        verdict = step.guard or {}
        self.diagnostics.event("guard", decision=decision, step=step.index, feature=step.text,
                               state=step.state_line, tools=step.sent_tools,
                               calls=copy.deepcopy(calls if calls is not None else step.calls),
                               issues=copy.deepcopy(verdict.get("issues", [])),
                               told=(step.help or {}).get("text", ""))

    def _guard_meta(self, step: Step) -> dict | None:
        verdict = step.guard or {}
        if verdict.get("decision") in (None, "pass"):
            return None
        return {"decision": verdict["decision"],
                "codes": sorted({issue.get("code", "") for issue in verdict.get("issues", [])})}

    def _check_calls(self, step: Step, calls) -> list[dict]:
        if not isinstance(calls, list) or not calls:
            raise ActionError("a step needs at least one call (agree with the empty answer instead)")
        if len(calls) > MAX_CALLS:
            raise ActionError(f"at most {MAX_CALLS} calls per step")
        allowed = step.sent_tools or [tool["name"] for tool in self.model.catalogue()]
        clean = []
        for call in calls:
            if not isinstance(call, dict) or not isinstance(call.get("name"), str):
                raise ActionError("each call needs a tool name")
            name, arguments = call["name"], call.get("arguments") or {}
            if not isinstance(arguments, dict):
                raise ActionError(f"{name}: arguments must be an object")
            if name not in allowed:
                raise ActionError(f"{name} was not offered to the model in this step, so the row could not hold it")
            problem = self.backend.validate(name, arguments)
            if problem:
                raise ActionError(problem)
            clean.append({"name": name, "arguments": arguments})
        return clean

    def rewrite(self, index: int, text: str):
        """The user rewrites a feature; the loop continues from that feature with the new text."""
        self._require_idle()
        step = self._step(index)
        if step.status in ("done", "negative", "running", "dialog", "choice"):
            raise ActionError("this step can no longer be rewritten (undo it first)")
        text = (text or "").strip()
        if not text:
            raise ActionError("the feature text is empty")
        pieces = split_goal(text)
        if step.status == "empty" and pieces == [step.text]:
            raise ActionError("the same text gets the same answer; write it differently")
        step.text = pieces[0]
        step.reset()
        if len(pieces) > 1:                             # the rewrite itself held several features
            extra = [Step(0, piece) for piece in pieces[1:]]
            self.steps[index + 1:index + 1] = extra
            for position, item in enumerate(self.steps):
                item.index = position
        self._touch()
        if index == self._current_index():
            self._submit(self._advance)

    def accept_empty(self, index: int, category: str):
        """The user agrees that nothing should be called: log a negative row.

        Also for calls the guard held back: the model's calls were wrong and the
        right answer is ``[]`` (a corrected row, with the model's calls in ``meta``).
        """
        self._require_idle()
        step = self._step(index)
        if not (step.status == "empty" or (step.status == "proposed" and step.blocked)):
            raise ActionError("this step has neither an empty answer nor calls that were held back")
        if category not in NO_CALL_REASONS:
            raise ActionError(f"category must be one of {', '.join(NO_CALL_REASONS)}")
        self._submit(self._log_negative_then_advance, step, category)

    def skip(self, index: int):
        """Abandon a feature. Nothing is logged; calls already executed for it are undone."""
        self._require_idle()
        step = self._step(index)
        if step.status in ("done", "negative", "skipped", "running"):
            raise ActionError("this step cannot be skipped")
        self._submit(self._skip, step)

    def resume(self, index: int):
        """The user closed the command dialog: continue with the call that was held back."""
        self._require_idle()
        step = self._step(index)
        if step.status != "dialog":
            raise ActionError("this step is not waiting for a dialog")
        self._submit(self._execute_then_advance, step)

    def proceed(self, index: int):
        """The dialog check could not be read and the user takes the risk, for this step only.

        Nothing is remembered beyond the step: the next step is checked again
        and stops again if its answer is unreadable too. An answer that *can*
        be read and names an open dialog still stops the step.
        """
        self._require_idle()
        step = self._step(index)
        if step.status != "dialog" or not step.dialog_unknown:
            raise ActionError("this step is not waiting on an unreadable dialog check")
        step.dialog_override = True
        step.notes.append("You chose to proceed although Fusion's answer to the dialog check could not be read; "
                          "the calls of this step were sent without that check.")
        if self.diagnostics is not None:
            self.diagnostics.event("dialog_check_overridden", step=step.index, feature=step.text,
                                   call=step.calls[step.next_call]["name"] if step.next_call < len(step.calls) else "")
        self._submit(self._execute_then_advance, step)

    def run_exclusive(self, activity: str, function):
        """Run ``function()`` on the worker, so it never overlaps a step (one Fusion call at a time)."""
        def task():
            self._set(activity=activity)
            function()

        self._submit(task)

    def choose(self, index: int, file_id):
        """The user picked one of several documents found for open_document."""
        self._require_idle()
        step = self._step(index)
        if step.status != "choice":
            raise ActionError("this step is not waiting for a choice")
        if file_id not in [entry["id"] for entry in step.choice]:
            raise ActionError("that document was not among the results")
        step.chosen = file_id
        self._submit(self._execute_then_advance, step)

    def retry(self, index: int):
        self._require_idle()
        step = self._step(index)
        if step.status not in ("failed", "error"):
            raise ActionError("this step did not fail")
        self._submit(self._retry, step)

    def undo_last_step(self):
        self._require_idle()
        if self._last_done() is None:
            raise ActionError("no executed step to undo")
        self._submit(self._undo_last_step)

    def undo_once(self):
        self._require_idle()
        self._submit(self._undo_once)

    def refresh(self):
        self._require_idle()
        self._submit(self._refresh_state)

    def commit_pending(self):
        """Write the row of the last executed step: the user has moved on."""
        with self._lock:
            pending, self._pending = self._pending, None
        if pending is None:
            return
        step, row = pending
        try:
            step.row_line = self.log.append(row)
            step.row_status = "logged"
        except OSError as failure:
            step.row_status = "not_logged"
            step.row_note = f"could not write the log: {failure}"
        self._touch()

    def close(self):
        self._stop = True
        self.wait(30)
        self.commit_pending()

    # ---- Fusion --------------------------------------------------------
    def _read_state(self) -> tuple[str, list]:
        result = self.fusion.call(self.backend.state_call())
        if not result["ok"]:
            raise McpError(f"reading the design state failed: {result['error']}")
        output = result["raw"]                          # the project's parser reads the server's own shape
        line = self.backend.parse_state(output)
        return line, self.backend.unhealthy(output)

    def _refresh_state(self) -> bool:
        self._set(activity="Reading the Fusion state")
        try:
            self.fusion_state, self.fusion_unhealthy = self._read_state()
            self.fusion_state_error = ""
            return True
        except Exception as failure:
            self.fusion_state_error = str(failure)
            return False
        finally:
            self._touch()

    def _keep_images(self, images: list[dict]) -> list[dict]:
        kept = []
        for image in images:
            self._image_seq += 1
            key = f"img{self._image_seq}"
            self.images[key] = image
            kept.append({"id": key, "mime": image.get("mime", "image/png")})
        while len(self.images) > MAX_IMAGES:
            self.images.pop(next(iter(self.images)))
        return kept

    def _capture_options(self) -> dict:
        """Image size and background for capture_view, from the settings (Fusion accepts 32 to 4096)."""
        def pixels(key: str, default: int) -> int:
            value = self.settings.get(key, default)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                value = default
            return max(32, min(4096, int(value)))

        return {"width": pixels("capture_width", 1280), "height": pixels("capture_height", 720),
                "transparentBackground": bool(self.settings.get("capture_transparent", False))}

    def _undo(self, count: int) -> tuple[int, str]:
        """Send ``count`` undos. -> (how many succeeded, first error)."""
        sent = 0
        for _ in range(count):
            try:
                result = self.fusion.call(self.backend.undo_call())
            except McpError as failure:
                return sent, str(failure)
            if not result["ok"]:
                return sent, result["error"]
            sent += 1
        return sent, ""

    # ---- the loop ------------------------------------------------------
    def _advance(self):
        while not self._stop:
            index = self._current_index()
            if index is None:
                self.auto = False
                return
            step = self.steps[index]
            if step.status != "pending":
                return
            self._propose(step)
            if step.status != "proposed":
                return                                  # empty answer or an error: the user decides
            if not self.auto or step.confirm or step.blocked or self._stop:
                return                                  # wait for approval
            self._execute(step)
            if step.status != "done":
                return

    def _retry(self, step: Step):
        step.reset()
        self._advance()

    def _execute_then_advance(self, step: Step):
        self._execute(step)
        if step.status == "done":
            self._advance()

    def _choose_tools(self, step: Step) -> list[str] | None:
        mode = self.settings.get("toolset", "auto")
        if mode == "catalogue":
            return None
        catalogue = self.model.catalogue()
        names = self.backend.pick_tools(step.text, step.state_line, [tool["name"] for tool in catalogue],
                                        self.model.max_tools())
        if names is None and mode == "router":
            step.notes.append("no router available (backend/router.py): tools omitted, Needle retrieves 5"
                              + (f" ({self.backend.router_error})" if self.backend.router_error else ""))
        return names

    def _propose(self, step: Step):
        step.reset()
        self._set(step, "reading", "Reading the Fusion state")
        try:
            step.state_line, step.unhealthy = self._read_state()
            self.fusion_state, self.fusion_unhealthy, self.fusion_state_error = step.state_line, step.unhealthy, ""
        except Exception as failure:
            step.error = f"Fusion: {failure}"
            self._set(step, "error")
            return
        step.sent_tools = self._choose_tools(step)
        self._set(step, "asking", "Asking the model")
        started = time.perf_counter()
        try:
            answer = self.model.step(step.text, step.state_line, step.sent_tools)
        except Exception as failure:
            step.error = f"Model: {failure}"
            self._set(step, "error")
            return
        step.model_ms = round((time.perf_counter() - started) * 1000, 1)
        calls = answer.get("calls") if isinstance(answer.get("calls"), list) else []
        step.answer = {**answer, "calls": calls, "suppressed": answer.get("suppressed") or []}
        if answer.get("engine_error") or not calls:
            step.answer["calls"] = []                   # engine_error: treated like an empty answer
            step.help = self._refusal(step)
            self._guard_event(step, "no_call")
            self._set(step, "empty")
            return
        step.calls = copy.deepcopy(calls)
        reasons = []
        if step.answer["suppressed"]:
            reasons.append(f"the model withheld {len(step.answer['suppressed'])} call(s) for lack of evidence")
        limit = int(self.settings.get("confirm_over_calls", 3))
        if len(calls) > limit:
            reasons.append(f"{len(calls)} calls for one feature")
        step.guard = self._judge(step, calls)
        if step.guard["decision"] != "pass":            # nothing of this answer is sent to Fusion as it is
            step.blocked = True
            step.help = self._refusal(step, calls, step.guard)
        self._guard_event(step, step.guard["decision"])
        for call in calls:
            name = call.get("name") if isinstance(call, dict) else None
            if isinstance(name, str) and self.backend.call_info(name)["confirm"]:
                reasons.append(f"{name} needs your confirmation")
        step.confirm = reasons
        self._set(step, "proposed")

    def _execute(self, step: Step):
        """Run ``step.calls`` from ``step.next_call`` on. One Fusion call at a time."""
        self.commit_pending()                           # the previous step is accepted
        step.dialog, step.error, step.error_call = "", "", None
        step.dialog_unknown, step.dialog_raw = False, ""
        self._set(step, "running", "Running in Fusion")
        while step.next_call < len(step.calls):
            position = step.next_call
            call = step.calls[position]
            name, arguments = call["name"], call.get("arguments") or {}
            info = self.backend.call_info(name)
            try:
                payload = self.backend.render(name, arguments, self.export_dir)
                payload = self.backend.with_capture_options(payload, self._capture_options())
            except Exception as failure:
                self._fail(step, position, f"{name}: {failure}")
                return
            if not info["read_only"]:
                try:
                    answer = self.fusion.call(self.backend.active_command_call())
                except McpError as failure:
                    self._fail(step, position, f"Fusion: {failure}")
                    return
                check = self.backend.dialog_check(answer)
                if check["unknown"] and not step.dialog_override:
                    # fail closed: an unreadable answer is never taken for "no dialog"
                    step.dialog = f"the answer to the dialog check could not be read ({check['reason']})"
                    step.dialog_unknown = True
                    step.dialog_raw = raw_text(answer.get("raw") if answer.get("raw") is not None
                                               else {"error": answer.get("error")})
                    if self.diagnostics is not None:
                        self.diagnostics.event("dialog_check_unreadable", step=step.index, feature=step.text,
                                               call=name, reason=check["reason"], answer=answer.get("raw"),
                                               error=answer.get("error"))
                    self._set(step, "dialog")
                    return
                if check["open"]:
                    step.dialog = check["name"] or "a command"
                    self._set(step, "dialog")
                    return
            self._set(activity=f"Running {name} in Fusion ({position + 1} of {len(step.calls)})")
            started = time.perf_counter()
            try:
                result = self.fusion.call(payload)
            except McpError as failure:
                result = {"ok": False, "text": "", "images": [], "data": None, "error": f"Fusion: {failure}"}
            entry = {"name": name, "ok": result["ok"], "text": result["text"], "error": result["error"],
                     "images": self._keep_images(result["images"]), "via": payload.get("name"),
                     "read_only": info["read_only"],
                     "ms": round((time.perf_counter() - started) * 1000, 1)}
            del step.results[position:], step.payloads[position:]
            step.results.append(entry)
            step.payloads.append(payload)
            if not result["ok"]:
                self._fail(step, position, result["error"] or "the call failed")
                return
            matches = self.backend.document_matches(name, payload, result)
            if matches is not None:                     # open_document: the call rendered a search
                if not matches:
                    self._fail(step, position, "no document matches that name")
                    return
                if len(matches) > 1 and step.chosen is None:
                    step.choice = matches
                    self._set(step, "choice")
                    return
                file_id, step.chosen, step.choice = step.chosen or matches[0]["id"], None, []
                try:
                    opened = self.fusion.call(self.backend.open_document_call(file_id))
                except Exception as failure:
                    opened = {"ok": False, "error": f"Fusion: {failure}", "text": ""}
                entry["text"] = (entry["text"] + "\n" + (opened.get("text") or "")).strip()
                if not opened["ok"]:
                    entry["ok"], entry["error"] = False, opened["error"]
                    self._fail(step, position, opened["error"] or "the document did not open")
                    return
            # rollback sends one undo per executed call that left an undo step
            # (not read-only calls, not exports / document tools, not the model's own undo / redo)
            if self.backend.switches_document(name, payload):
                step.undo_count = 0                     # another document is active now:
                for earlier in self.steps[:step.index + 1]:
                    earlier.locked = True               # never roll back across the switch
            elif self.backend.leaves_undo_step(info, payload):
                step.undo_count += 1
            step.next_call = position + 1
            self._touch()
        self._set(activity="Reading the Fusion state")
        try:
            step.state_after, unhealthy = self._read_state()
            self.fusion_state, self.fusion_unhealthy, self.fusion_state_error = step.state_after, unhealthy, ""
            if unhealthy:
                step.notes.append("Fusion reports timeline items with errors or warnings: "
                                  + ", ".join(str(item.get("name", item)) if isinstance(item, dict) else str(item)
                                              for item in unhealthy))
        except Exception as failure:
            self.fusion_state_error = str(failure)
            step.notes.append(f"the state could not be read after the step: {failure}")
        self._make_row(step)
        self._set(step, "done")

    def _fail(self, step: Step, position: int, message: str):
        """Undo once per call of this step that already succeeded, then stop."""
        step.error, step.error_call = message, position
        if step.undo_count:
            self._set(activity="Undoing the calls of this step")
            step.undone, problem = self._undo(step.undo_count)
            if problem:
                step.notes.append(f"undo stopped after {step.undone} of {step.undo_count}: {problem}")
            step.undo_count -= step.undone
        self._set(step, "failed")                       # the loop stops here; "Try again" keeps the mode
        self._refresh_state()

    # ---- rows ----------------------------------------------------------
    def _row(self, step: Step, answers: list[dict], category: str, status: str) -> dict:
        self._row_seq += 1
        answer = step.answer or {}
        tools = offered_schemas(step.sent_tools, answers, self.model.catalogue(), self.model.max_tools())
        corrected = status == "corrected"
        row = self.log.build_row(
            query=step.text, system=step.state_line, tools=tools, answers=copy.deepcopy(answers),
            reasoning="" if corrected else str(answer.get("reasoning") or ""),
            goal_id=self.goal_id, number=self._row_seq, category=category, status=status,
            model_answers=copy.deepcopy(answer.get("calls", [])) if corrected else None)
        self.log.model_name = self.model.name() or self.log.model_name
        row["meta"]["model"] = self.log.model_name
        guard = self._guard_meta(step)
        if guard is not None:
            row["meta"]["guard"] = guard
        step.row_id = row["meta"]["id"]
        return row

    def _checked(self, step: Step, row: dict) -> bool:
        """Is the session history switched on? (Settings, "Session history"; off by default.)"""
        return bool(self.settings.get("save_history", False))

    def _make_row(self, step: Step):
        status = "corrected" if step.corrected else "executed"
        row = self._row(step, step.calls, category_of(step.calls), status)
        verdict = step.guard or {}
        unbacked = [issue["message"] for issue in verdict.get("issues", [])
                    if verdict.get("decision") == "overridden" or issue.get("code") in RULE_CODES]
        if verdict.get("decision") in ("overridden", "edited") and unbacked:
            # labels are literal: a call with a value the request does not write is no history entry
            step.row_status = "not_logged"
            step.row_note = "not kept as a history entry, the request does not back the calls: " + " ".join(unbacked)
            return
        if self._checked(step, row):
            step.row_status = "pending"
            self._pending = (step, row)

    def _log_negative_then_advance(self, step: Step, category: str):
        self.commit_pending()
        held_back = step.status == "proposed"           # the guard held the model's calls back
        if held_back:
            step.guard = {**(step.guard or {}), "decision": "blocked"}
            step.calls, step.corrected, step.blocked, step.confirm = [], True, False, []
            self._guard_event(step, "agreed_no_call", [])
        row = self._row(step, [], category, "corrected" if held_back else "executed")
        if self._checked(step, row):
            try:
                step.row_line = self.log.append(row)
                step.row_status = "logged"
            except OSError as failure:
                step.row_status, step.row_note = "not_logged", f"could not write the log: {failure}"
        self._set(step, "negative")
        self._advance()

    # ---- undo / skip ---------------------------------------------------
    def _skip(self, step: Step):
        if step.undo_count:                             # a step held back by a dialog had calls done
            self._set(activity="Undoing the calls of this step")
            step.undone, problem = self._undo(step.undo_count)
            if problem:
                step.notes.append(f"undo stopped after {step.undone} of {step.undo_count}: {problem}")
            step.undo_count -= step.undone
            self._refresh_state()
        self._set(step, "skipped")
        self._advance()                                 # step mode: this only proposes the next feature

    def _withdraw_row(self, step: Step, why: str):
        if self._pending is not None and self._pending[0] is step:
            self._pending = None
            step.row_status, step.row_note = "withdrawn", why
        elif step.row_status == "logged":
            step.row_note = (f"{why}; its row is already line {step.row_line} of {self.log.path.name} "
                             "(the log is append-only: delete that line by hand if the step was wrong)")

    def _undo_last_step(self):
        step = self._last_done()
        if step is None:
            return
        self.auto = False
        self._set(activity="Undoing the last step")
        count = step.undo_count
        sent, problem = self._undo(count)
        self._refresh_state()
        if problem:
            step.undo_count -= sent
            step.notes.append(f"undo stopped after {sent} of {count}: {problem}")
            self.message = f"Undo failed: {problem}"
            self._withdraw_row(step, "partly undone")
            self._touch()
            return
        self._withdraw_row(step, "step undone")
        note, row_status, row_line, row_id = step.row_note, step.row_status, step.row_line, step.row_id
        step.reset()
        if row_status in ("logged", "withdrawn"):
            step.notes.append(f"previous run undone ({count} undo(s) in Fusion)" + (f": {note}" if note else ""))
        if row_status == "logged":
            step.row_status, step.row_line, step.row_id = "logged", row_line, row_id
        for later in self.steps[step.index + 1:]:       # proposals made on the old state are stale
            if later.status in ("proposed", "empty", "error"):
                later.reset()
        self._touch()

    def _undo_once(self):
        """One raw Fusion undo. The design no longer matches the last step, so its row is withdrawn."""
        self._set(activity="Undo in Fusion")
        sent, problem = self._undo(1)
        if problem:
            self.message = f"Undo failed: {problem}"
        else:
            last = self._last_done()
            if last is not None:
                self._withdraw_row(last, "undone by hand after the step")
            for step in self.steps:
                if step.status in ("proposed", "empty", "error"):
                    step.reset()
        self._refresh_state()
