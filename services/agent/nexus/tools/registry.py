"""Tool registry: schemas, permission gating, validation, audit logging.

Every tool call goes through Registry.execute():
1. Unknown tool -> ToolError.
2. Permission check against policy table (default per-tool level).
   - OBSERVE: always allowed.
   - ASSIST: never executes (returns a suggestion payload instead).
   - EXECUTE: allowed.
   - ELEVATED: requires an approver callback returning True.
3. Argument validation against the tool's JSON schema (subset).
4. Execution with timeout; result + audit row written either way.
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass
from typing import Any, Callable

from ..core.errors import NexusError, ToolError, ValidationError
from ..core.logging import get_logger
from ..core.types import Permission, ToolResult, ToolStatus

log = get_logger("tools.registry")

Handler = Callable[[dict[str, Any]], dict[str, Any]]
Approver = Callable[[str, dict[str, Any]], bool]


@dataclass
class ToolSpec:
    name: str
    description: str
    schema: dict[str, Any]              # JSON-schema subset for arguments
    handler: Handler
    default_permission: Permission = Permission.OBSERVE
    timeout_s: float = 60.0


def _validate(schema: dict, args: dict, path: str = "args") -> None:
    """Minimal JSON-schema validation: types, required, properties, enums."""
    if not isinstance(args, dict):
        raise ValidationError(f"{path} must be an object")
    for req in schema.get("required", []):
        if req not in args:
            raise ValidationError(f"{path}.{req} is required")
    props = schema.get("properties", {})
    for key, value in args.items():
        spec = props.get(key)
        if spec is None:
            if schema.get("additionalProperties") is False:
                raise ValidationError(f"{path}.{key} is not allowed")
            continue
        t = spec.get("type")
        if t and not _type_ok(t, value):
            raise ValidationError(f"{path}.{key} must be {t}")
        if "enum" in spec and value not in spec["enum"]:
            raise ValidationError(f"{path}.{key} must be one of {spec['enum']}")


def _type_ok(t: str, v: Any) -> bool:
    return {"string": isinstance(v, str), "integer": isinstance(v, int) and not isinstance(v, bool),
            "number": isinstance(v, (int, float)) and not isinstance(v, bool),
            "boolean": isinstance(v, bool), "object": isinstance(v, dict),
            "array": isinstance(v, list), "null": v is None}.get(t, True)


class ToolRegistry:
    def __init__(self, db_conn, default_timeout_s: float = 60.0):
        self._tools: dict[str, ToolSpec] = {}
        self._conn = db_conn
        self._default_timeout = default_timeout_s
        self._lock = threading.Lock()

    def register(self, spec: ToolSpec) -> None:
        self._tools[spec.name] = spec
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO permission_policies(tool_name, level) VALUES (?,?)",
                (spec.name, spec.default_permission.value))
            self._conn.commit()

    def set_policy(self, tool_name: str, level: Permission) -> None:
        if tool_name not in self._tools:
            raise ToolError(f"unknown tool '{tool_name}'")
        with self._lock:
            self._conn.execute(
                "INSERT INTO permission_policies(tool_name, level) VALUES (?,?)"
                " ON CONFLICT(tool_name) DO UPDATE SET level=excluded.level",
                (tool_name, level.value))
            self._conn.commit()

    def policy(self, tool_name: str) -> Permission:
        row = self._conn.execute(
            "SELECT level FROM permission_policies WHERE tool_name=?",
            (tool_name,)).fetchone()
        if row:
            return Permission(row["level"])
        return self._tools[tool_name].default_permission

    def specs_for_model(self) -> list[dict]:
        """OpenAI-style tool schemas for the provider call."""
        out = []
        for spec in self._tools.values():
            if self.policy(spec.name) == Permission.ASSIST:
                continue  # assist tools are suggestion-only; not offered to the model
            out.append({"type": "function",
                        "function": {"name": spec.name,
                                     "description": spec.description,
                                     "parameters": spec.schema}})
        return out

    def validate(self, name: str, arguments: dict) -> None:
        """Validate arguments against a tool's schema. Raises on problems."""
        spec = self._tools.get(name)
        if spec is None:
            raise NexusError(f"unknown tool '{name}'")
        _validate(spec.schema, arguments)

    def execute(self, name: str, arguments: dict, task_id: str | None = None,
                approver: Approver | None = None,
                approved_by: str | None = None) -> ToolResult:
        call_id = uuid.uuid4().hex[:12]
        spec = self._tools.get(name)
        if spec is None:
            return ToolResult(call_id, name, ToolStatus.ERROR, error=f"unknown tool '{name}'")
        level = self.policy(name)

        if level == Permission.ASSIST:
            return ToolResult(call_id, name, ToolStatus.DENIED, error=(
                "tool is ASSIST-only: it may suggest actions but never executes. "
                "Ask the user to approve, then run via an EXECUTE-level tool."))

        if level == Permission.ELEVATED:
            ok = approver(name, arguments) if approver else False
            if not ok:
                self._audit(task_id, name, arguments, level, None,
                            ToolStatus.DENIED, {"reason": "elevated action not approved"})
                return ToolResult(call_id, name, ToolStatus.DENIED,
                                  error="elevated action requires approval",
                                  needs_approval=True)

        try:
            _validate(spec.schema, arguments)
        except ValidationError as e:
            self._audit(task_id, name, arguments, level, approved_by,
                        ToolStatus.ERROR, {"error": str(e)})
            return ToolResult(call_id, name, ToolStatus.ERROR, error=str(e))

        timeout = min(spec.timeout_s, self._default_timeout)
        start = time.monotonic()
        try:
            with ThreadPoolExecutor(max_workers=1) as ex:
                data = ex.submit(spec.handler, arguments).result(timeout=timeout)
            status, err = ToolStatus.OK, None
        except FutureTimeout:
            data, status, err = {}, ToolStatus.TIMEOUT, f"timed out after {timeout}s"
        except Cancelled_:
            data, status, err = {}, ToolStatus.CANCELLED, "cancelled"
        except Exception as e:
            data, status, err = {}, ToolStatus.ERROR, f"{type(e).__name__}: {e}"
        duration_ms = int((time.monotonic() - start) * 1000)
        self._audit(task_id, name, arguments, level, approved_by, status, data, duration_ms)
        return ToolResult(call_id, name, status, data=data, error=err, duration_ms=duration_ms)

    def _audit(self, task_id, name, arguments, level, approved_by, status, data, duration_ms=0):
        with self._lock:
            # Link to the task only when its row exists (standalone planner
            # use may pass an id with no row yet — never fail the audit).
            if task_id is not None:
                exists = self._conn.execute(
                    "SELECT 1 FROM tasks WHERE id=?", (task_id,)).fetchone()
                if not exists:
                    task_id = None
            self._conn.execute(
                """INSERT INTO tool_executions(task_id, tool_name, args_json, permission,
                       approved_by, status, result_json, duration_ms)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (task_id, name, json.dumps(arguments), level.value, approved_by,
                 status.value, json.dumps(data)[:8000], duration_ms))
            self._conn.execute(
                "INSERT INTO audit_events(category, summary, detail_json) VALUES (?,?,?)",
                ("tool", f"{name} -> {status.value}",
                 json.dumps({"task_id": task_id, "permission": level.value})[:2000]))
            self._conn.commit()


class Cancelled_(Exception):
    """Internal marker so tool threads can cooperate with cancellation."""
