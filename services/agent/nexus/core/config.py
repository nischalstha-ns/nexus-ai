"""Configuration from environment variables. No secrets in code, ever."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List

from .errors import ConfigError


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError as e:
        raise ConfigError(f"{name} must be an integer") from e


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except ValueError as e:
        raise ConfigError(f"{name} must be a number") from e


@dataclass
class Settings:
    # Paths
    data_dir: str = field(default_factory=lambda: os.environ.get("NEXUS_DATA_DIR", "./data"))
    # Model routing
    default_provider: str = field(default_factory=lambda: os.environ.get("NEXUS_PROVIDER", "mock"))
    offline_only: bool = field(default_factory=lambda: os.environ.get("NEXUS_OFFLINE_ONLY", "0") == "1")
    request_timeout_s: float = field(default_factory=lambda: _float("NEXUS_REQUEST_TIMEOUT_S", 60.0))
    max_retries: int = field(default_factory=lambda: _int("NEXUS_MAX_RETRIES", 2))
    # Planner bounds
    max_iterations: int = field(default_factory=lambda: _int("NEXUS_MAX_ITERATIONS", 12))
    task_timeout_s: float = field(default_factory=lambda: _float("NEXUS_TASK_TIMEOUT_S", 300.0))
    tool_timeout_s: float = field(default_factory=lambda: _float("NEXUS_TOOL_TIMEOUT_S", 60.0))
    # Tool sandbox
    approved_roots: List[str] = field(default_factory=lambda: [
        p for p in os.environ.get("NEXUS_APPROVED_ROOTS", "").split(os.pathsep) if p
    ])
    allowed_commands: List[str] = field(default_factory=lambda: [
        c for c in os.environ.get("NEXUS_ALLOWED_COMMANDS", "ls,dir,echo,python,python3,pytest,git").split(",") if c
    ])
    # Server
    host: str = field(default_factory=lambda: os.environ.get("NEXUS_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: _int("NEXUS_PORT", 8765))

    @property
    def db_path(self) -> str:
        return os.path.join(self.data_dir, "nexus.db")
