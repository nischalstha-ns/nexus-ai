"""SQLite store: connection handling + versioned migrations from data/migrations/."""
from __future__ import annotations

import os
import sqlite3

from ..core.logging import get_logger

log = get_logger("memory.store")


class Store:
    def __init__(self, db_path: str, migrations_dir: str):
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self.db_path = db_path
        self.migrations_dir = migrations_dir
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        # NOTE: with check_same_thread=False the connection may only be used
        # from one thread at a time — callers must serialize access (the
        # service layer holds a lock around every use).
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self.migrate()

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn

    def applied_versions(self) -> set[int]:
        try:
            rows = self._conn.execute("SELECT version FROM schema_migrations").fetchall()
            return {r["version"] for r in rows}
        except sqlite3.OperationalError:
            return set()

    def migrate(self) -> None:
        applied = self.applied_versions()
        files = sorted(f for f in os.listdir(self.migrations_dir) if f.endswith(".sql"))
        for fname in files:
            try:
                version = int(fname.split("_")[0])
            except ValueError:
                continue
            if version in applied:
                continue
            path = os.path.join(self.migrations_dir, fname)
            log.info("applying migration %s", fname)
            with open(path, encoding="utf-8") as f:
                sql = f.read()
            with self._conn:  # transaction per migration
                self._conn.executescript(sql)
                self._conn.execute(
                    "INSERT OR IGNORE INTO schema_migrations(version) VALUES (?)", (version,))
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()
