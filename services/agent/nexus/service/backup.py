"""Backup & restore for the agent database.

Uses sqlite's online backup API — no downtime, no restart needed.
Backups live in <data_dir>/backups/ as nexus-YYYYMMDD-HHMMSS[-label].db
plus a .json manifest with row counts for a sanity check on restore.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time

from ..core.errors import NexusError
from ..core.logging import get_logger

log = get_logger("service.backup")

_SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
EXPECTED_TABLES = {"conversations", "messages", "memories", "tasks",
                   "tool_executions", "providers"}


def _backups_dir(data_dir: str) -> str:
    d = os.path.join(data_dir, "backups")
    os.makedirs(d, exist_ok=True)
    return d


def _check_db(path: str) -> dict:
    """Sanity-check that a file is a NEXUS database."""
    with open(path, "rb") as f:
        if f.read(16) != b"SQLite format 3\x00":
            raise NexusError("not a SQLite database file")
    conn = sqlite3.connect(path)
    try:
        tables = {r[0] for r in
                  conn.execute("SELECT name FROM sqlite_master"
                               " WHERE type='table'").fetchall()}
    finally:
        conn.close()
    missing = EXPECTED_TABLES - tables
    if missing:
        raise NexusError(f"not a NEXUS database (missing: {sorted(missing)})")
    return {"tables": sorted(tables)}


def row_counts(conn) -> dict:
    out = {}
    for t in sorted(EXPECTED_TABLES):
        try:
            out[t] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        except Exception:
            out[t] = None
    return out


def backup(live_conn, data_dir: str, label: str = "") -> dict:
    """Online backup of the live database. Returns manifest info."""
    label = (label or "").strip()
    if label and not _SAFE.match(label):
        raise NexusError("label may only contain letters, digits, ., _, -")
    d = _backups_dir(data_dir)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    name = f"nexus-{stamp}" + (f"-{label}" if label else "") + ".db"
    path = os.path.join(d, name)
    # Commit any pending writes first: sqlite's backup API blocks while the
    # source connection holds an uncommitted write transaction.
    try:
        live_conn.commit()
    except Exception:
        pass
    dest = sqlite3.connect(path)
    try:
        live_conn.backup(dest)
    finally:
        dest.close()
    info = _check_db(path)
    manifest = {"name": name, "created": stamp, "label": label,
                "bytes": os.path.getsize(path),
                "row_counts": row_counts(live_conn), **info}
    with open(path + ".json", "w") as f:
        json.dump(manifest, f, indent=2)
    log.info("backup written: %s (%d bytes)", path, manifest["bytes"])
    return manifest


def list_backups(data_dir: str) -> list[dict]:
    d = _backups_dir(data_dir)
    out = []
    for fn in sorted(os.listdir(d)):
        if fn.endswith(".db"):
            mf = os.path.join(d, fn + ".json")
            if os.path.isfile(mf):
                out.append(json.loads(open(mf).read()))
            else:
                out.append({"name": fn, "bytes": os.path.getsize(
                    os.path.join(d, fn))})
    return out


def restore(live_conn, data_dir: str, name: str) -> dict:
    """Restore a backup INTO the live connection (no restart needed)."""
    if not _SAFE.match(name) or not name.endswith(".db"):
        raise NexusError("invalid backup name")
    path = os.path.join(_backups_dir(data_dir), name)
    if not os.path.isfile(path):
        raise NexusError(f"backup '{name}' not found")
    _check_db(path)  # refuse to restore garbage
    try:
        live_conn.commit()
    except Exception:
        pass
    src = sqlite3.connect(path)
    try:
        src.backup(live_conn)
    finally:
        src.close()
    counts = row_counts(live_conn)
    log.info("restored backup %s into live database", name)
    return {"ok": True, "name": name, "row_counts": counts}


if __name__ == "__main__":
    import argparse
    from ..core.config import Settings
    ap = argparse.ArgumentParser(description="NEXUS database backup tool")
    ap.add_argument("action", choices=["backup", "list", "restore"])
    ap.add_argument("--label", default="")
    ap.add_argument("--name", default="")
    args = ap.parse_args()
    s = Settings()
    conn = sqlite3.connect(s.db_path)
    try:
        if args.action == "backup":
            print(json.dumps(backup(conn, s.data_dir, args.label), indent=2))
        elif args.action == "list":
            print(json.dumps(list_backups(s.data_dir), indent=2))
        else:
            if not args.name:
                ap.error("--name is required for restore")
            print(json.dumps(restore(conn, s.data_dir, args.name), indent=2))
    finally:
        conn.close()
