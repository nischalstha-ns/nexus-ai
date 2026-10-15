"""Planning + execution engine: the bounded observe → act → verify loop.

Flow per user request:
1. Ask the model for the next step (text answer, tool call, or a control signal).
2. Control signals the model may emit via a tool call to `planner_control`:
   - {"action": "done", "summary": ...}        -> finish successfully
   - {"action": "need_input", "question": ...} -> pause for the user
3. Otherwise execute the tool call, OBSERVE the result, VERIFY it against
   expectations, and feed the outcome back.
4. Bounds: max_iterations, per-action retry budget, wall-clock timeout,
   cancellation event, loop detection (same tool+args 3x in a row).

Approval flow: tools at ELEVATED level need an approver. In non-interactive
mode a denial ends the turn as awaiting_approval. In interactive mode the
planner yields an `approval_request` event and pauses until
submit_approval() is called (e.g. by the HTTP service when the user taps
Approve/Deny), then resumes with the decision.

The model NEVER runs code directly; it only proposes tool calls, which are
permission-gated by the registry.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Iterator

from ..core.errors import BudgetExceeded
from ..core.logging import get_logger
from ..core.types import (AgentEvent, Message, Permission, PlanStep, Role,
                          TaskStatus, ToolStatus)
from ..router.router import ModelRouter
from ..tools.registry import Approver, ToolRegistry, ToolSpec

log = get_logger("planner")

SYSTEM_PROMPT = """You are NEXUS, a personal AI agent. You solve tasks by calling tools.
Rules:
- Think step by step, then either call exactly one tool or finish.
- To finish, call planner_control with {"action": "done", "summary": "..."}.
- If you need information only the user has, call planner_control with
  {"action": "need_input", "question": "..."}.
- Never claim you did something you did not do. Report only observed results.
- Keep tool arguments minimal and valid."""


def verify_step(step: PlanStep, result) -> tuple[bool, str]:
    """Independent verification of a tool result against expectations.

    Returns (ok, note). This is the VERIFY in observe-act-verify: it does
    not trust the tool's own status alone.
    """
    if result.status != ToolStatus.OK:
        return False, f"tool reported {result.status.value}: {result.error}"
    name, args, data = step.tool or "", step.arguments, result.data or {}
    if name == "write_file":
        path = args.get("path", "")
        if path and os.path.isfile(path):
            return True, f"verified: {path} exists"
        return False, f"verify failed: {path} not found after write"
    if name == "run_command":
        rc = data.get("returncode")
        if rc == 0:
            return True, "verified: exit code 0"
        return False, f"verify failed: exit code {rc}"
    if name == "capture_screen":
        path = data.get("path", "")
        if path and os.path.isfile(path):
            return True, "verified: screenshot file exists"
        return False, "verify failed: screenshot file missing"
    return True, "no verifier for this tool; trusted tool status"


class Planner:
    def __init__(self, router: ModelRouter, tools: ToolRegistry,
                 max_iterations: int = 12, task_timeout_s: float = 300.0,
                 max_step_retries: int = 2, approval_timeout_s: float = 300.0):
        self.router = router
        self.tools = tools
        self.max_iterations = max_iterations
        self.task_timeout_s = task_timeout_s
        self.max_step_retries = max_step_retries
        self.approval_timeout_s = approval_timeout_s
        self._cancel = threading.Event()
        self._approval_event = threading.Event()
        self._approval: tuple[bool, str | None] | None = None
        # planner_control is engine-owned, not user-registered
        tools.register(ToolSpec(
            name="planner_control",
            description="Control the task loop: done | need_input.",
            schema={"type": "object",
                    "properties": {"action": {"type": "string",
                                              "enum": ["done", "need_input"]},
                                   "summary": {"type": "string"},
                                   "question": {"type": "string"}},
                    "required": ["action"], "additionalProperties": False},
            handler=lambda a: {"ack": a["action"]},
            default_permission=Permission.OBSERVE,
        ))

    def cancel(self) -> None:
        self._cancel.set()

    def submit_approval(self, approved: bool, by: str | None = None) -> None:
        """Deliver a user's approval decision to a paused planner.

        Safe to call while the planner is suspended at the approval_request
        yield: the decision is consumed when the generator is resumed.
        """
        self._approval = (approved, by)
        self._approval_event.set()

    def run(self, user_text: str, history: list[Message] | None = None,
            approver: Approver | None = None,
            require_local: bool = False,
            task_id: str | None = None,
            interactive_approval: bool = False) -> Iterator[AgentEvent]:
        """Yields AgentEvents; the final event has kind 'done' with a summary."""
        self._cancel.clear()
        self._approval_event.clear()
        self._approval = None
        deadline = time.monotonic() + self.task_timeout_s
        messages = [Message(Role.SYSTEM, SYSTEM_PROMPT)]
        messages.extend(history or [])
        messages.append(Message(Role.USER, user_text))

        plan: list[PlanStep] = []
        recent: list[tuple[str, str]] = []  # loop detection: (tool, args-json)
        attempts: dict[tuple[str, str], int] = {}  # retry budget per action
        status = TaskStatus.RUNNING
        summary = ""

        yield AgentEvent("status", {"status": status.value, "task_id": task_id})

        for i in range(self.max_iterations):
            if self._cancel.is_set():
                yield AgentEvent("done", {"status": TaskStatus.CANCELLED.value,
                                         "summary": "cancelled by user",
                                         "steps": [s.__dict__ for s in plan]})
                return
            if time.monotonic() > deadline:
                raise BudgetExceeded(f"task exceeded {self.task_timeout_s}s")

            resp = self.router.complete(messages, tools=self.tools.specs_for_model(),
                                        require_local=require_local)
            if resp.text:
                messages.append(Message(Role.ASSISTANT, resp.text))
                yield AgentEvent("message", {"text": resp.text})

            if not resp.tool_calls:
                summary = resp.text or "no further action proposed"
                status = TaskStatus.DONE
                break

            call = resp.tool_calls[0]  # one tool per iteration: observable + bounded
            yield AgentEvent("tool_call", {"name": call.name, "arguments": call.arguments})

            if call.name == "planner_control":
                action = call.arguments.get("action")
                if action == "done":
                    summary = call.arguments.get("summary", "")
                    status = TaskStatus.DONE
                    break
                if action == "need_input":
                    status = TaskStatus.AWAITING_INPUT
                    summary = call.arguments.get("question", "")
                    yield AgentEvent("done", {"status": status.value, "summary": summary,
                                             "steps": [s.__dict__ for s in plan]})
                    return

            sig = (call.name, str(sorted(call.arguments.items())))
            recent.append(sig)
            if len(recent) >= 3 and recent[-1] == recent[-2] == recent[-3]:
                summary = (f"stopped: tool '{call.name}' repeated 3x with identical "
                           f"arguments (loop detected)")
                status = TaskStatus.FAILED
                yield AgentEvent("error", {"error": summary})
                break

            attempts[sig] = attempts.get(sig, 0) + 1
            if attempts[sig] > self.max_step_retries + 1:
                note = (f"retry budget exhausted for '{call.name}' "
                        f"({self.max_step_retries} retries); not executing again")
                yield AgentEvent("error", {"error": note})
                messages.append(Message(
                    Role.TOOL,
                    str({"status": "error", "error": note}), tool_name=call.name))
                continue

            step = PlanStep(id=f"s{i}", description=f"{call.name}",
                            tool=call.name, arguments=call.arguments,
                            status="running", attempts=attempts[sig])
            plan.append(step)
            yield AgentEvent("plan", {"steps": [s.__dict__ for s in plan]})

            result = self.tools.execute(call.name, call.arguments,
                                        task_id=task_id, approver=approver)

            # Interactive approval: pause for the user instead of failing.
            if (result.needs_approval and interactive_approval
                    and result.status == ToolStatus.DENIED):
                self._approval_event.clear()
                self._approval = None
                yield AgentEvent("approval_request",
                                 {"task_id": task_id, "tool": call.name,
                                  "arguments": call.arguments,
                                  "call_id": result.call_id})
                log.info("waiting for approval of '%s' (task %s)",
                         call.name, task_id)
                got = self._approval_event.wait(timeout=self.approval_timeout_s)
                if got and self._approval is not None:
                    approved, by = self._approval
                    yield AgentEvent("approval_decision",
                                     {"approved": approved, "by": by})
                    if approved:
                        result = self.tools.execute(
                            call.name, call.arguments, task_id=task_id,
                            approver=lambda n, a: True, approved_by=by)
                    else:
                        result.error = (result.error or "") + " (denied by user)"
                else:
                    result.error = (result.error or "") + " (approval timed out)"
                    status = TaskStatus.AWAITING_APPROVAL
                    summary = result.error or "approval needed"

            # VERIFY: independent check of the outcome.
            if result.status == ToolStatus.OK:
                ok, note = verify_step(step, result)
                step.verify_note = note
                if not ok:
                    result.status = ToolStatus.ERROR
                    result.error = note
            step.status = "done" if result.status == ToolStatus.OK else "failed"

            yield AgentEvent("tool_result", {"name": result.name, "status": result.status.value,
                                            "data": result.data, "error": result.error,
                                            "verify": step.verify_note})
            obs = {"status": result.status.value}
            if result.data:
                obs["data"] = result.data
            if result.error:
                obs["error"] = result.error
            if step.verify_note:
                obs["verify"] = step.verify_note
            messages.append(Message(Role.TOOL, str(obs), tool_name=result.name))

            if result.needs_approval and result.status == ToolStatus.DENIED \
                    and not interactive_approval:
                status, summary = TaskStatus.AWAITING_APPROVAL, result.error or "approval needed"
                break
        else:
            status, summary = TaskStatus.FAILED, f"stopped after {self.max_iterations} iterations"

        yield AgentEvent("done", {"status": status.value, "summary": summary,
                                 "steps": [s.__dict__ for s in plan]})
