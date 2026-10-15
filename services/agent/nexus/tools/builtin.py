"""Built-in tools. All filesystem access is jailed to approved roots;
shell commands must come from an explicit allowlist. No exceptions.
"""
from __future__ import annotations

import fnmatch
import os
import re
import subprocess
from datetime import datetime, timezone

from ..core.types import Permission
from .registry import ToolSpec


def _resolve(root_allow: list[str], path: str) -> str:
    """Resolve path and ensure it stays inside one of the approved roots."""
    abs_p = os.path.abspath(os.path.expanduser(path))
    for root in root_allow:
        abs_r = os.path.abspath(os.path.expanduser(root))
        if abs_p == abs_r or abs_p.startswith(abs_r + os.sep):
            return abs_p
    raise PermissionError(f"path '{path}' is outside approved roots")


def builtin_tools(approved_roots: list[str], allowed_commands: list[str]) -> list[ToolSpec]:
    roots = approved_roots or [os.path.expanduser("~")]

    def now(args):
        return {"utc": datetime.now(timezone.utc).isoformat()}

    def list_files(args):
        d = _resolve(roots, args.get("path", roots[0]))
        entries = []
        for name in sorted(os.listdir(d)):
            p = os.path.join(d, name)
            entries.append({"name": name, "dir": os.path.isdir(p),
                            "size": os.path.getsize(p) if os.path.isfile(p) else 0})
        return {"path": d, "entries": entries[:500]}

    def read_file(args):
        p = _resolve(roots, args["path"])
        if not os.path.isfile(p):
            raise FileNotFoundError(p)
        max_chars = int(args.get("max_chars", 20000))
        with open(p, encoding="utf-8", errors="replace") as f:
            return {"path": p, "content": f.read(max_chars)}

    def write_file(args):
        p = _resolve(roots, args["path"])
        os.makedirs(os.path.dirname(p), exist_ok=True)
        content = args["content"]
        if not args.get("overwrite", False) and os.path.exists(p):
            raise FileExistsError(f"{p} exists; pass overwrite=true to replace")
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)
        return {"path": p, "bytes": len(content.encode())}

    def search_files(args):
        base = _resolve(roots, args.get("path", roots[0]))
        pattern, hits = args["pattern"], []
        for dirpath, _, filenames in os.walk(base):
            for fn in filenames:
                if not fnmatch.fnmatch(fn, args.get("glob", "*")):
                    continue
                fp = os.path.join(dirpath, fn)
                try:
                    with open(fp, encoding="utf-8", errors="ignore") as f:
                        for i, line in enumerate(f, 1):
                            if re.search(pattern, line):
                                hits.append({"file": fp, "line": i,
                                             "text": line.strip()[:300]})
                                if len(hits) >= 100:
                                    return {"hits": hits}
                except OSError:
                    continue
        return {"hits": hits}

    def run_command(args):
        cmd = args["command"]
        if isinstance(cmd, str):
            parts = cmd.split()
        else:
            parts = list(cmd)
        if not parts or parts[0] not in allowed_commands:
            raise PermissionError(
                f"command '{parts[0] if parts else ''}' not in allowlist: {allowed_commands}")
        cwd = _resolve(roots, args.get("cwd", roots[0]))
        proc = subprocess.run(parts, cwd=cwd, capture_output=True, text=True,
                              timeout=int(args.get("timeout_s", 30)))
        return {"returncode": proc.returncode, "stdout": proc.stdout[-8000:],
                "stderr": proc.stderr[-8000:]}

    S = lambda n, d, props, req=(), perm=Permission.OBSERVE, h=None, **kw: ToolSpec(
        name=n, description=d,
        schema={"type": "object", "properties": props, "required": list(req),
                "additionalProperties": False},
        handler=h, default_permission=perm, **kw)

    return [
        S("get_time", "Current UTC date/time.", {}, h=now),
        S("list_files", "List a directory inside approved roots.",
          {"path": {"type": "string"}}, h=list_files),
        S("read_file", "Read a text file inside approved roots.",
          {"path": {"type": "string"}, "max_chars": {"type": "integer"}}, ("path",), h=read_file),
        S("write_file", "Write a file inside approved roots.",
          {"path": {"type": "string"}, "content": {"type": "string"},
           "overwrite": {"type": "boolean"}}, ("path", "content"),
          perm=Permission.EXECUTE, h=write_file),
        S("search_files", "Regex-search file contents inside approved roots.",
          {"path": {"type": "string"}, "pattern": {"type": "string"},
           "glob": {"type": "string"}}, ("pattern",), h=search_files),
        S("run_command", "Run an allowlisted command (no shell).",
          {"command": {"type": "string"}, "cwd": {"type": "string"},
           "timeout_s": {"type": "integer"}}, ("command",),
          perm=Permission.EXECUTE, h=run_command),
    ]
