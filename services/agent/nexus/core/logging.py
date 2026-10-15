"""Logging: human-readable by default, JSON with NEXUS_LOG_JSON=1."""
from __future__ import annotations

import logging
import os
import sys

_configured = False


def get_logger(name: str) -> logging.Logger:
    global _configured
    if not _configured:
        handler = logging.StreamHandler(sys.stderr)
        if os.environ.get("NEXUS_LOG_JSON") == "1":
            handler.setFormatter(logging.Formatter(
                '{"ts":"%(asctime)s","level":"%(levelname)s","name":"%(name)s","msg":%(message)s}'
            ))
        else:
            handler.setFormatter(logging.Formatter(
                "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                datefmt="%H:%M:%S",
            ))
        root = logging.getLogger("nexus")
        root.addHandler(handler)
        root.setLevel(os.environ.get("NEXUS_LOG_LEVEL", "INFO").upper())
        _configured = True
    logger = logging.getLogger(f"nexus.{name}")
    # Never log secrets: providers must redact before logging.
    return logger


def redact(text: str) -> str:
    """Best-effort redaction of bearer tokens in a log line."""
    import re
    return re.sub(r"(?i)(bearer\s+)[A-Za-z0-9\-._~+/=]+", r"\1***", text)
