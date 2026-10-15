"""Workflow learning: record demonstrations, edit them, replay them safely.

A workflow is a named list of {tool, arguments} steps. The honest way to
build one is RECORDING: take a task the agent actually completed and
snapshot the tool calls that worked. The editor can then trim, reorder,
or re-parameterize steps; replay runs them through the same
permission-gated registry with per-step verification, optional dry-run,
and file checkpoints so a bad replay can be reverted.
"""
from __future__ import annotations

import json
import os
import shutil
import uuid
from datetime import datetime, timezone

from ..core.errors import NexusError
from ..core.logging import get_logger
from ..core.types import Permission, TaskStatus, ToolStatus
from ..planner.engine import verify_step
from ..core.types import PlanStep
from ..tools.registry import Approver, ToolRegistry

log = get_logger("workflows")

_NON_RECORDABLE = {"planner_control"}


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class WorkflowService:
    def __init__(self, store_conn, registry: ToolRegistry, data_dir: str):
        self.conn = store_conn
        self.registry = registry
        self.data_dir = data_dir
        self.checkpoint_root = os.path.join(data_dir, "checkpoints")
        os.makedirs(self.checkpoint_root, exist_ok=True)

    # -- recording & editing --
    def validate_steps(self, steps: list[dict]) -> None:
        if not isinstance(steps, list) or not steps:
            raise NexusError("workflow needs a non-empty list of steps")
        for i, s in enumerate(steps):
            if not isinstance(s, dict) or "tool" not in s:
                raise NexusError(f"step {i}: must be {{tool, arguments?}}")
            args = s.get("arguments", {})
            if not isinstance(args, dict):
                raise NexusError(f"step {i}: arguments must be an object")
            try:
                self.registry.validate(s["tool"], args)
            except NexusError as e:
                raise NexusError(f"step {i} ({s['tool']}): {e}") from e

    def record_from_task(self, task_id: str, name: str,
                         description: str = "") -> dict:
        task = self.conn.execute(
            "SELECT id FROM tasks WHERE id=?", (task_id,)).fetchone()
        if task is None:
            raise NexusError(f"unknown task '{task_id}'")
        rows = self.conn.execute(
            "SELECT tool_name, args_json FROM tool_executions"
            " WHERE task_id=? AND status='ok' ORDER BY id",
            (task_id,)).fetchall()
        steps = [{"tool": r["tool_name"], "arguments": json.loads(r["args_json"])}
                 for r in rows if r["tool_name"] not in _NON_RECORDABLE]
        if not steps:
            raise NexusError("task has no successful tool executions to record")
        return self.create_workflow(name, description, steps,
                                    created_from_task=task_id)

    def create_workflow(self, name: str, description: str,
                        steps: list[dict], created_from_task: str | None = None) -> dict:
        if not name or not name.strip():
            raise NexusError("workflow name is required")
        self.validate_steps(steps)
        wf_id = uuid.uuid4().hex[:12]
        self.conn.execute(
            "INSERT INTO workflows(id, name, description, steps_json, created_from_task)"
            " VALUES (?,?,?,?,?)",
            (wf_id, name.strip(), description or "", json.dumps(steps),
             created_from_task))
        self.conn.commit()
        log.info("recorded workflow '%s' (%d steps)", name, len(steps))
        return self.get_workflow(wf_id)

    def update_workflow(self, wf_id: str, name: str | None = None,
                        description: str | None = None,
                        steps: list[dict] | None = None) -> dict:
        wf = self.get_workflow(wf_id)  # raises if unknown
        row = self.conn.execute(
            "SELECT steps_json FROM workflows WHERE id=?", (wf_id,)).fetchone()
        steps_json = row["steps_json"]
        if steps is not None:
            self.validate_steps(steps)
            steps_json = json.dumps(steps)
        if name is not None:
            if not name.strip():
                raise NexusError("workflow name cannot be empty")
            wf["name"] = name.strip()
        if description is not None:
            wf["description"] = description
        self.conn.execute(
            "UPDATE workflows SET name=?, description=?, steps_json=?,"
            " updated_at=? WHERE id=?",
            (wf["name"], wf["description"], steps_json, _utcnow(), wf_id))
        self.conn.commit()
        return self.get_workflow(wf_id)

    def delete_workflow(self, wf_id: str) -> None:
        cur = self.conn.execute("DELETE FROM workflows WHERE id=?", (wf_id,))
        self.conn.commit()
        if cur.rowcount == 0:
            raise NexusError(f"unknown workflow '{wf_id}'")

    def list_workflows(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT id, name, description, created_from_task, created_at, updated_at,"
            " json_array_length(steps_json) AS steps"
            " FROM workflows ORDER BY updated_at DESC").fetchall()
        return [dict(r) for r in rows]

    def get_workflow(self, wf_id: str) -> dict:
        row = self.conn.execute(
            "SELECT * FROM workflows WHERE id=?", (wf_id,)).fetchone()
        if row is None:
            raise NexusError(f"unknown workflow '{wf_id}'")
        d = dict(row)
        d["steps"] = json.loads(d.pop("steps_json"))
        return d

    # -- safe replay --
    def replay(self, wf_id: str, approver: Approver | None = None,
               approved_by: str | None = None,
               dry_run: bool = False, checkpoint: bool = True) -> dict:
        """Execute a workflow's steps in order through the gated registry.

        dry_run: validate everything, execute nothing.
        checkpoint: back up files that write_file steps will touch, so a
        failed replay can be reverted with revert_run().
        Stops at the first failed/denied step.
        """
        wf = self.get_workflow(wf_id)
        steps: list[dict] = wf["steps"]
        run_id = uuid.uuid4().hex[:12]
        task_id = uuid.uuid4().hex[:12]
        self.conn.execute(
            "INSERT INTO tasks(id, title, status) VALUES (?,?,?)",
            (task_id, f"replay: {wf['name']}", TaskStatus.RUNNING.value))
        ckpt_dir = os.path.join(self.checkpoint_root, run_id) if checkpoint else None
        if ckpt_dir:
            os.makedirs(ckpt_dir, exist_ok=True)
        self.conn.execute(
            "INSERT INTO workflow_runs(id, workflow_id, task_id, dry_run, checkpoint_dir)"
            " VALUES (?,?,?,?,?)",
            (run_id, wf_id, task_id, int(dry_run), ckpt_dir))
        self.conn.commit()

        results: list[dict] = []
        status = TaskStatus.DONE
        failed_at: int | None = None
        try:
            for i, s in enumerate(steps):
                tool, args = s["tool"], s.get("arguments", {})
                level = self.registry.policy(tool)
                if level == Permission.ASSIST:
                    raise NexusError(f"step {i}: '{tool}' is ASSIST-only")
                if level == Permission.ELEVATED and not dry_run:
                    ok = approver(tool, args) if approver else False
                    if not ok:
                        status = TaskStatus.AWAITING_APPROVAL
                        failed_at = i
                        results.append({"step": i, "tool": tool,
                                        "status": "awaiting_approval",
                                        "error": "elevated step needs approval"})
                        break
                if dry_run:
                    self.registry.validate(tool, args)
                    entry = {"step": i, "tool": tool, "status": "dry_run_ok"}
                    if level == Permission.ELEVATED:
                        entry["approval"] = "required"
                    results.append(entry)
                    continue
                if ckpt_dir and tool == "write_file":
                    self._checkpoint(ckpt_dir, i, args.get("path", ""))
                res = self.registry.execute(tool, args, task_id=task_id,
                                            approver=approver,
                                            approved_by=approved_by)
                entry = {"step": i, "tool": tool,
                         "status": res.status.value, "error": res.error,
                         "duration_ms": res.duration_ms}
                # verify, same as the planner does
                if res.status == ToolStatus.OK:
                    ok, note = verify_step(
                        PlanStep(id=f"w{i}", description=tool, tool=tool,
                                 arguments=args), res)
                    entry["verify"] = note
                    if not ok:
                        res.status = ToolStatus.ERROR
                        res.error = note
                        entry["status"] = "error"
                        entry["error"] = note
                results.append(entry)
                if res.status != ToolStatus.OK:
                    status = TaskStatus.FAILED
                    failed_at = i
                    break
        except NexusError as e:
            status = TaskStatus.FAILED
            results.append({"error": str(e)})
        except Exception as e:  # never leave a run stuck at 'running'
            log.exception("workflow replay %s failed", run_id)
            status = TaskStatus.FAILED
            results.append({"error": f"internal error: {type(e).__name__}"})

        self.conn.execute(
            "UPDATE workflow_runs SET status=?, result_json=?, updated_at=?"
            " WHERE id=?",
            (status.value, json.dumps({"steps": results, "failed_at": failed_at}),
             _utcnow(), run_id))
        self.conn.execute(
            "UPDATE tasks SET status=?, updated_at=? WHERE id=?",
            (status.value, _utcnow(), task_id))
        self.conn.commit()
        log.info("replayed workflow '%s' -> %s (%d steps)",
                 wf["name"], status.value, len(results))
        return {"run_id": run_id, "task_id": task_id, "status": status.value,
                "dry_run": dry_run, "steps": results, "failed_at": failed_at}

    def _checkpoint(self, ckpt_dir: str, index: int, path: str) -> None:
        """Back up a file before a write_file step overwrites it."""
        manifest_path = os.path.join(ckpt_dir, "manifest.json")
        manifest = {}
        if os.path.isfile(manifest_path):
            manifest = json.loads(open(manifest_path).read())
        entry = {"path": path, "existed": False, "backup": None}
        if path and os.path.isfile(path):
            bak = os.path.join(ckpt_dir, f"step{index}.bak")
            shutil.copy2(path, bak)
            entry.update(existed=True, backup=bak)
        manifest[str(index)] = entry
        with open(manifest_path, "w") as f:
            json.dump(manifest, f)

    def revert_run(self, run_id: str) -> dict:
        """Restore files checkpointed during a replay run."""
        row = self.conn.execute(
            "SELECT * FROM workflow_runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise NexusError(f"unknown run '{run_id}'")
        ckpt_dir = row["checkpoint_dir"]
        if not ckpt_dir:
            raise NexusError("run was recorded without checkpoints")
        manifest_path = os.path.join(ckpt_dir, "manifest.json")
        if not os.path.isfile(manifest_path):
            raise NexusError("checkpoint manifest missing")
        manifest = json.loads(open(manifest_path).read())
        restored, removed = [], []
        for entry in manifest.values():
            path = entry["path"]
            if entry["existed"] and entry["backup"] \
                    and os.path.isfile(entry["backup"]):
                shutil.copy2(entry["backup"], path)
                restored.append(path)
            elif not entry["existed"] and path and os.path.isfile(path):
                os.remove(path)
                removed.append(path)
        self.conn.execute(
            "UPDATE workflow_runs SET status=?, updated_at=? WHERE id=?",
            ("reverted", _utcnow(), run_id))
        self.conn.commit()
        log.info("reverted run %s: %d restored, %d removed",
                 run_id, len(restored), len(removed))
        return {"run_id": run_id, "restored": restored, "removed": removed}

    def get_run(self, run_id: str) -> dict:
        row = self.conn.execute(
            "SELECT * FROM workflow_runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise NexusError(f"unknown run '{run_id}'")
        d = dict(row)
        d["result"] = json.loads(d.pop("result_json"))
        return d
