"""Shared typed contracts for the NEXUS agent service.

Everything crossing a module boundary uses these dataclasses — no bare dicts.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class Role(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    TOOL = "tool"


class Permission(str, Enum):
    OBSERVE = "observe"      # read-only: screen info, file reads in approved dirs
    ASSIST = "assist"        # may suggest, never executes
    EXECUTE = "execute"      # explicitly permitted low-risk actions
    ELEVATED = "elevated"    # consequential: always requires confirmation


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    AWAITING_INPUT = "awaiting_input"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ToolStatus(str, Enum):
    OK = "ok"
    ERROR = "error"
    DENIED = "denied"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"


@dataclass
class Message:
    role: Role
    content: str
    tool_name: Optional[str] = None


@dataclass
class ToolCall:
    name: str
    arguments: Dict[str, Any] = field(default_factory=dict)
    call_id: str = ""


@dataclass
class ToolResult:
    call_id: str
    name: str
    status: ToolStatus
    data: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    duration_ms: int = 0
    needs_approval: bool = False  # True when an approver could still allow it


@dataclass
class ProviderResponse:
    text: str = ""
    tool_calls: List[ToolCall] = field(default_factory=list)
    model: str = ""
    usage: Dict[str, int] = field(default_factory=dict)
    latency_ms: int = 0


@dataclass
class PlanStep:
    id: str
    description: str
    tool: Optional[str] = None
    arguments: Dict[str, Any] = field(default_factory=dict)
    requires_approval: bool = False
    status: str = "pending"  # pending|running|done|failed|skipped
    attempts: int = 0
    verify_note: Optional[str] = None  # outcome of the verify check


@dataclass
class AgentEvent:
    kind: str  # message|tool_call|tool_result|plan|approval_request|status|error|done
    payload: Dict[str, Any] = field(default_factory=dict)
