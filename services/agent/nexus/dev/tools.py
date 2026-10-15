"""Developer-agent tools for the registry.

- index_project / search_code / find_symbol: OBSERVE (read-only)
- propose_patch: OBSERVE (computes a diff, writes nothing)
- apply_patch: EXECUTE (writes files, all-or-nothing)
- run_tests: EXECUTE (allowlisted test command, no shell, cwd=root)
"""
from __future__ import annotations

import os
import re
import subprocess

from ..core.errors import NexusError
from ..core.types import Permission
from ..tools.builtin import _resolve
from ..tools.registry import ToolSpec
from .indexer import ProjectIndexer
from .patcher import Patcher


class DevContext:
    def __init__(self, conn, approved_roots: list[str],
                 allowed_commands: list[str], default_test_command: str = ""):
        self.indexer = ProjectIndexer(conn, approved_roots)
        self.patcher = Patcher(conn, approved_roots)
        self.roots = approved_roots
        self.allowed_commands = allowed_commands
        self.default_test_command = default_test_command


def dev_tools(ctx: DevContext) -> list[ToolSpec]:
    def index_project(args):
        return ctx.indexer.index(args.get("root", ctx.roots[0] if ctx.roots else "~"))

    def find_symbol(args):
        return {"matches": ctx.indexer.find_symbol(
            args.get("root", ctx.roots[0] if ctx.roots else "~"),
            args["name"], int(args.get("limit", 20)))}

    def search_code(args):
        base = _resolve(ctx.roots, args.get("root", ctx.roots[0] if ctx.roots else "~"))
        pattern = args.get("pattern", "")
        try:
            rx = re.compile(pattern)
        except re.error as e:
            raise NexusError(f"bad regex: {e}")
        hits, truncated = [], False
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames
                           if d not in {".git", "__pycache__", "node_modules"}]
            for fn in filenames:
                full = os.path.join(dirpath, fn)
                try:
                    with open(full, encoding="utf-8", errors="replace") as f:
                        for ln, line in enumerate(f, 1):
                            if rx.search(line):
                                hits.append({"path": os.path.relpath(full, base),
                                             "line": ln,
                                             "text": line.strip()[:200]})
                                if len(hits) >= 100:
                                    truncated = True
                                    break
                except OSError:
                    continue
                if truncated:
                    break
            if truncated:
                break
        return {"hits": hits, "truncated": truncated}

    def propose_patch(args):
        return ctx.patcher.propose(
            args.get("root", ctx.roots[0] if ctx.roots else "~"),
            args["edits"])

    def apply_patch(args):
        return ctx.patcher.apply(args["patch_id"])

    def discard_patch(args):
        ctx.patcher.discard(args["patch_id"])
        return {"ok": True}

    def run_tests(args):
        root = _resolve(ctx.roots, args.get("root", ctx.roots[0] if ctx.roots else "~"))
        command = (args.get("command") or ctx.default_test_command
                   or "python -m unittest discover").strip()
        parts = command.split()
        if not parts or parts[0] not in ctx.allowed_commands:
            raise NexusError(
                f"command '{parts[0] if parts else ''}' not in allowlist: "
                f"{ctx.allowed_commands}")
        if not os.path.isdir(root):
            raise NexusError(f"not a directory: {root}")
        try:
            proc = subprocess.run(parts, cwd=root, capture_output=True,
                                  timeout=float(args.get("timeout_s", 300)))
        except subprocess.TimeoutExpired:
            raise NexusError("tests timed out")
        out = (proc.stdout.decode(errors="replace")
               + proc.stderr.decode(errors="replace"))[-8000:]
        return {"root": root, "command": command,
                "returncode": proc.returncode, "passed": proc.returncode == 0,
                "output": out}

    S = lambda n, d, props, req=(), perm=Permission.OBSERVE, h=None: ToolSpec(
        name=n, description=d,
        schema={"type": "object", "properties": props, "required": list(req),
                "additionalProperties": False},
        handler=h, default_permission=perm)

    return [
        S("index_project",
          "Index a project root (jailed): file list + Python symbols.",
          {"root": {"type": "string"}}, h=index_project),
        S("find_symbol",
          "Find where a function/class name is defined in the index.",
          {"root": {"type": "string"}, "name": {"type": "string"},
           "limit": {"type": "integer"}}, ("name",), h=find_symbol),
        S("search_code",
          "Regex-search file contents under a project root (max 100 hits).",
          {"root": {"type": "string"}, "pattern": {"type": "string"}},
          ("pattern",), h=search_code),
        S("propose_patch",
          "Propose code edits; returns a patch id + unified diff preview. "
          "Writes nothing.",
          {"root": {"type": "string"},
           "edits": {"type": "array"}}, ("edits",), h=propose_patch),
        S("apply_patch",
          "Apply a proposed patch (all-or-nothing; rejects stale patches).",
          {"patch_id": {"type": "string"}}, ("patch_id",),
          perm=Permission.EXECUTE, h=apply_patch),
        S("discard_patch",
          "Discard a proposed patch without applying it.",
          {"patch_id": {"type": "string"}}, ("patch_id",), h=discard_patch),
        S("run_tests",
          "Run the project's test command (allowlisted, no shell) and "
          "return pass/fail + output.",
          {"root": {"type": "string"}, "command": {"type": "string"},
           "timeout_s": {"type": "number"}}, h=run_tests,
          perm=Permission.EXECUTE),
    ]
