"""Patch proposal + application with diff previews.

Two-step safety pattern:
1. propose_patch(edits) — validates, computes the new file contents, renders
   a unified diff, stores the patch as 'proposed'. Nothing is written.
2. apply_patch(patch_id) — re-reads the files, requires every old_text to
   still match EXACTLY ONCE (stale patches are rejected, never half-applied),
   then writes all files and marks the patch 'applied'.

All paths are jailed to approved roots, like the file tools.
"""
from __future__ import annotations

import difflib
import json
import os
import uuid
from datetime import datetime, timezone

from ..core.errors import NexusError
from ..core.logging import get_logger
from ..tools.builtin import _resolve

log = get_logger("dev.patcher")


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def render_diff(path: str, old: str, new: str) -> str:
    return "".join(difflib.unified_diff(
        old.splitlines(keepends=True), new.splitlines(keepends=True),
        fromfile=f"a/{path}", tofile=f"b/{path}"))


class Patcher:
    def __init__(self, conn, approved_roots: list[str]):
        self.conn = conn
        self.roots = approved_roots

    def _read(self, path: str) -> str:
        p = _resolve(self.roots, path)
        if not os.path.isfile(p):
            raise NexusError(f"not a file: {path}")
        with open(p, encoding="utf-8", errors="replace") as f:
            return f.read()

    def propose(self, root: str, edits: list[dict]) -> dict:
        """Validate edits and return a stored patch with a diff preview."""
        _resolve(self.roots, root)  # jail check
        if not edits:
            raise NexusError("edits must be a non-empty list")
        norm = []
        diffs = []
        for i, e in enumerate(edits):
            for f_ in ("path", "old_text", "new_text"):
                if f_ not in e:
                    raise NexusError(f"edit {i}: '{f_}' is required")
            old = self._read(e["path"])
            count = old.count(e["old_text"])
            if count == 0:
                raise NexusError(f"edit {i}: old_text not found in {e['path']}")
            if count > 1:
                raise NexusError(
                    f"edit {i}: old_text matches {count}x in {e['path']} — "
                    f"make it unique")
            new = old.replace(e["old_text"], e["new_text"], 1)
            norm.append({"path": e["path"], "old_text": e["old_text"],
                         "new_text": e["new_text"]})
            diffs.append(render_diff(e["path"], old, new))
        patch_id = uuid.uuid4().hex[:12]
        diff_text = "\n".join(diffs)
        self.conn.execute(
            "INSERT INTO patches(id, root, edits_json, diff_text)"
            " VALUES (?,?,?,?)",
            (patch_id, os.path.abspath(root), json.dumps(norm), diff_text))
        self.conn.commit()
        log.info("proposed patch %s (%d edits)", patch_id, len(norm))
        return {"id": patch_id, "status": "proposed", "edits": norm,
                "diff": diff_text}

    def apply(self, patch_id: str) -> dict:
        row = self.conn.execute(
            "SELECT * FROM patches WHERE id=?", (patch_id,)).fetchone()
        if row is None:
            raise NexusError(f"unknown patch '{patch_id}'")
        if row["status"] != "proposed":
            raise NexusError(f"patch is {row['status']}, not proposed")
        edits = json.loads(row["edits_json"])
        # Verify ALL edits first — all-or-nothing.
        new_contents = []
        for i, e in enumerate(edits):
            old = self._read(e["path"])
            count = old.count(e["old_text"])
            if count != 1:
                self.conn.execute(
                    "UPDATE patches SET status='stale' WHERE id=?", (patch_id,))
                self.conn.commit()
                raise NexusError(
                    f"patch went stale: edit {i} matches {count}x in "
                    f"{e['path']} (file changed since proposal)")
            new_contents.append((e["path"], old.replace(
                e["old_text"], e["new_text"], 1)))
        written = []
        for path, new in new_contents:
            p = _resolve(self.roots, path)
            with open(p, "w", encoding="utf-8") as f:
                f.write(new)
            written.append(path)
        self.conn.execute(
            "UPDATE patches SET status='applied', applied_at=? WHERE id=?",
            (_utcnow(), patch_id))
        self.conn.commit()
        log.info("applied patch %s (%d files)", patch_id, len(written))
        return {"id": patch_id, "status": "applied", "files": written}

    def discard(self, patch_id: str) -> None:
        cur = self.conn.execute(
            "UPDATE patches SET status='discarded' WHERE id=? AND status='proposed'",
            (patch_id,))
        self.conn.commit()
        if cur.rowcount == 0:
            raise NexusError(f"no proposed patch '{patch_id}'")

    def get(self, patch_id: str) -> dict:
        row = self.conn.execute(
            "SELECT * FROM patches WHERE id=?", (patch_id,)).fetchone()
        if row is None:
            raise NexusError(f"unknown patch '{patch_id}'")
        d = dict(row)
        d["edits"] = json.loads(d.pop("edits_json"))
        return d

    def list(self, limit: int = 30) -> list[dict]:
        rows = self.conn.execute(
            "SELECT id, root, status, created_at, applied_at FROM patches"
            " ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]
