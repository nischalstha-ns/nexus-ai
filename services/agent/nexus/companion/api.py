"""Android companion protocol: secure pairing, token auth, notifications.

Security model:
- The service stays loopback-bound. Remote companion access is expected
  over an encrypted tunnel the USER controls (Tailscale, adb reverse).
  The service never opens a LAN port on its own.
- Pairing: the desktop UI shows a short-lived, single-use code
  (POST /pairing/code). The phone redeems it once for a long-lived
  device token (POST /pairing/redeem). Codes expire in 10 minutes.
- Only sha256 hashes are stored; raw codes/tokens are shown once.
- Companion endpoints require `Authorization: Bearer <device_token>`.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from ..core.errors import NexusError
from ..core.logging import get_logger

log = get_logger("companion")

_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no ambiguous chars
_CODE_TTL = timedelta(minutes=10)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hash(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


class CompanionService:
    def __init__(self, conn):
        self.conn = conn

    # -- pairing --
    def create_pairing_code(self) -> dict:
        code = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(8))
        expires = datetime.now(timezone.utc) + _CODE_TTL
        self.conn.execute(
            "INSERT INTO pairing_codes(code_hash, expires_at) VALUES (?,?)",
            (_hash(code), expires.isoformat()))
        self.conn.commit()
        log.info("pairing code created (expires %s)", expires.isoformat())
        return {"code": code, "expires_at": expires.isoformat()}

    def redeem(self, code: str, device_name: str) -> dict:
        if not device_name or not device_name.strip():
            raise NexusError("device_name is required")
        row = self.conn.execute(
            "SELECT * FROM pairing_codes WHERE code_hash=?", (_hash(code),)).fetchone()
        if row is None:
            raise NexusError("invalid pairing code")
        if row["used"]:
            raise NexusError("pairing code already used")
        if datetime.fromisoformat(row["expires_at"]) < datetime.now(timezone.utc):
            raise NexusError("pairing code expired")
        self.conn.execute("UPDATE pairing_codes SET used=1 WHERE code_hash=?",
                          (_hash(code),))
        token = secrets.token_urlsafe(32)
        device_id = uuid.uuid4().hex[:12]
        self.conn.execute(
            "INSERT INTO devices(id, name, token_hash) VALUES (?,?,?)",
            (device_id, device_name.strip()[:80], _hash(token)))
        self.conn.commit()
        log.info("device paired: %s (%s)", device_name, device_id)
        return {"device_id": device_id, "device_token": token,
                "warning": "store this token securely; it is shown only once"}

    def authenticate(self, token: str | None) -> dict | None:
        if not token:
            return None
        row = self.conn.execute(
            "SELECT * FROM devices WHERE token_hash=?", (_hash(token),)).fetchone()
        if row is None:
            return None
        self.conn.execute("UPDATE devices SET last_seen=? WHERE id=?",
                          (_utcnow(), row["id"]))
        self.conn.commit()
        return dict(row)

    def revoke(self, device_id: str) -> None:
        cur = self.conn.execute("DELETE FROM devices WHERE id=?", (device_id,))
        self.conn.commit()
        if cur.rowcount == 0:
            raise NexusError(f"unknown device '{device_id}'")

    # -- notifications --
    def notify(self, kind: str, title: str, body: str = "",
               payload: dict | None = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO notifications(kind, title, body, payload)"
            " VALUES (?,?,?,?)",
            (kind, title, body, json.dumps(payload or {})))
        self.conn.commit()
        nid = cur.lastrowid
        log.info("notify [%s] %s", kind, title)
        return nid

    def list_notifications(self, since_id: int = 0, limit: int = 50) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM notifications WHERE id>? ORDER BY id DESC LIMIT ?",
            (since_id, max(1, min(limit, 200)))).fetchall()
        return [dict(r) for r in rows]

    def mark_read(self, nid: int) -> None:
        self.conn.execute("UPDATE notifications SET read_at=? WHERE id=?",
                          (_utcnow(), nid))
        self.conn.commit()

    def unread_count(self) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) c FROM notifications WHERE read_at IS NULL").fetchone()
        return row["c"]
