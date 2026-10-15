"""NEXUS agent HTTP service (loopback only).

Endpoints:
  GET  /health
  POST /chat            {conversation_id?, message, require_local?}
  POST /chat/stream     same, but Server-Sent Events of planner activity
                           (interactive approvals: stream pauses at
                           approval_request; resume via /tasks/{id}/approve)
  GET  /tasks?limit=           (task history)
  GET  /tasks/{id}             (task detail + its tool executions)
  POST /tasks/{id}/approve {approved, by?} -> SSE of the resumed turn
  POST /tasks/{id}/cancel      (cancel a suspended or recorded task)
  GET  /workflows              (recorded workflows)
  POST /workflows       {name, description?, steps?} | {from_task, name}
  GET  /workflows/{id}
  PATCH /workflows/{id} {name?, description?, steps?}   (workflow editor)
  DELETE /workflows/{id}
  POST /workflows/{id}/replay {dry_run?, checkpoint?}   (safe replay)
  GET  /workflow-runs/{id}
  POST /workflow-runs/{id}/revert                       (restore checkpoints)
  GET  /dev/index?root=        (project index status)
  GET  /dev/patches            (proposed/applied patches)
  GET  /dev/patches/{id}       (patch detail + diff)
  POST /pairing/code            (one-time code for the companion app)
  POST /pairing/redeem {code, device_name} -> {device_token} (shown once)
  GET  /devices                 (paired devices)
  DELETE /devices/{id}          (revoke a device)
  -- companion (requires: Authorization: Bearer <device_token>) --
  GET  /companion/status        (device, pending approvals, unread count)
  GET  /companion/approvals     (tasks awaiting approval)
  POST /companion/approvals/{task_id} {approved}  (decide; turn resumes)
  GET  /companion/notifications?since=
  POST /companion/notifications/{id}/read
  POST /backup {label?}             (online DB backup, no downtime)
  GET  /backups                     (list backups)
  POST /restore {name}              (restore into the live DB)
  GET  /conversations
  POST /conversations   {title?}
  GET  /conversations/search?q=&limit=   (FTS over all message content)
  GET  /conversations/{id}/messages
  DELETE /conversations/{id}
  GET  /providers                 (never includes secrets)
  POST /providers       {id, kind, base_url, model_id, is_local?}
  DELETE /providers/{id}
  GET  /memories/search?q=
  POST /memories        {kind, content, consent?}
  GET  /memories/pending           (inferred-pending review queue)
  PATCH /memories/{id}  {content?, kind?}   (edit; consent unchanged)
  POST /memories/{id}/approve     (promote inferred-pending -> explicit)
  DELETE /memories/{id}
  GET  /export
  GET  /activity?limit=            (tool executions + audit timeline)
  GET  /permissions                (current tool permission levels)
  POST /permissions/{tool} {level} (change a tool's permission level)
  GET  /voice/status
  POST /voice/transcribe   (raw WAV bytes in body -> {text})
  POST /voice/synthesize   {text} -> audio/wav bytes

A chat turn runs the planner to completion (bounded). If it ends in
awaiting_input/awaiting_approval, the client sends the answer/decision as
the next message — no server-side suspended state.
"""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.parse
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .. import __version__
from ..core.config import Settings
from ..core.errors import BodyTooLarge, NexusError
from ..core.logging import get_logger
from ..core.secrets import SecretStore
from ..core.types import Permission, Role, TaskStatus
from ..memory.api import MemoryService
from ..memory.store import Store
from ..planner.engine import Planner
from ..providers.factory import load_providers
from ..router.router import ModelRouter
from ..tools.builtin import builtin_tools
from ..tools.registry import ToolRegistry
from ..voice.capture import SoundDeviceMic
from ..voice.pipeline import VoicePipeline
from ..voice.player import CommandPlayer, MockPlayer
from ..voice.stt import MockSTT, WhisperAPI
from ..voice.tts import CommandTTS, MockTTS
from ..perception.automation import MockAutomation
from ..perception.ocr import MockOCR, TesseractOCR
from ..perception.screencap import CommandScreenCap, MockScreen
from ..perception.tools import ComputerContext, computer_tools
from ..workflows.api import WorkflowService
from ..dev.tools import DevContext, dev_tools
from ..companion.api import CompanionService

log = get_logger("service")


class NexusService:
    def __init__(self, settings: Settings | None = None,
                 migrations_dir: str | None = None):
        self.settings = settings or Settings()
        if self.settings.host not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("refusing to bind a non-loopback host")
        import os
        migrations_dir = migrations_dir or os.path.join(
            os.path.dirname(__file__),  # .../services/agent/nexus/service
            "..", "..", "..", "..", "data", "migrations")
        self.store = Store(self.settings.db_path, os.path.abspath(migrations_dir))
        self.memory = MemoryService(self.store)
        self.secrets = SecretStore()
        self.registry = ToolRegistry(self.store.conn,
                                     default_timeout_s=self.settings.tool_timeout_s)
        for spec in builtin_tools(self.settings.approved_roots or [],
                                  self.settings.allowed_commands):
            self.registry.register(spec)
        self._reload_router()
        self._lock = threading.Lock()
        self._waiting: dict = {}  # task_id -> (ts, planner, gen, cid, events)
        self._init_voice()
        self._init_embeddings()
        self._init_computer()
        self.workflows = WorkflowService(self.store.conn, self.registry,
                                         self.settings.data_dir)
        self.companion = CompanionService(self.store.conn)
        self._init_dev()

    def _init_dev(self):
        import os
        ctx = DevContext(
            self.store.conn, self.settings.approved_roots or [os.path.expanduser("~")],
            self.settings.allowed_commands,
            default_test_command=os.environ.get("NEXUS_TEST_COMMAND", ""))
        self.dev = ctx
        for spec in dev_tools(ctx):
            self.registry.register(spec)
        log.info("dev tools ready (roots=%d)", len(ctx.roots))

    def _init_computer(self):
        import os
        data_dir = self.settings.data_dir
        screen_kind = os.environ.get("NEXUS_SCREEN", "mock").lower()
        screencap_cmd = os.environ.get("NEXUS_SCREENCAP_COMMAND", "")
        screen = CommandScreenCap(screencap_cmd) \
            if (screen_kind == "command" and screencap_cmd) else MockScreen()
        ocr_kind = os.environ.get("NEXUS_OCR", "mock").lower()
        ocr = TesseractOCR() if ocr_kind == "tesseract" else MockOCR()
        allowed_apps: dict[str, str] = {}
        for pair in os.environ.get("NEXUS_ALLOWED_APPS", "").split(";"):
            if "=" in pair:
                name, cmd = pair.split("=", 1)
                if name.strip() and cmd.strip():
                    allowed_apps[name.strip()] = cmd.strip()
        self.computer = ComputerContext(screen, ocr, MockAutomation(),
                                        data_dir, allowed_apps)
        for spec in computer_tools(self.computer):
            self.registry.register(spec)
        log.info("computer: screen=%s ocr=%s apps=%d",
                 screen.name, ocr.name, len(allowed_apps))

    def _init_embeddings(self):
        import os
        kind = os.environ.get("NEXUS_EMBEDDINGS", "mock").lower()
        if kind == "none":
            self.embedder = None
        elif kind == "openai":
            from ..memory.embeddings import OpenAIEmbeddings
            self.embedder = OpenAIEmbeddings(
                os.environ.get("NEXUS_EMBEDDINGS_URL", "https://api.openai.com/v1"),
                model=os.environ.get("NEXUS_EMBEDDINGS_MODEL", "text-embedding-3-small"),
                api_key=self.secrets.get("embeddings", env_var="NEXUS_EMBEDDINGS_API_KEY",
                                         required=False))
        else:
            from ..memory.embeddings import MockEmbeddings
            self.embedder = MockEmbeddings()
        log.info("embeddings: %s", self.embedder.name if self.embedder else "disabled")

    def _init_voice(self):
        import os
        stt_kind = os.environ.get("NEXUS_STT", "mock").lower()
        if stt_kind == "whisper":
            self.stt = WhisperAPI(
                os.environ.get("NEXUS_WHISPER_URL", "http://127.0.0.1:8000"),
                model=os.environ.get("NEXUS_WHISPER_MODEL", "whisper-1"),
                api_key=self.secrets.get("whisper", env_var="NEXUS_WHISPER_API_KEY",
                                         required=False))
        else:
            self.stt = MockSTT()
        tts_kind = os.environ.get("NEXUS_TTS", "mock").lower()
        tts_cmd = os.environ.get("NEXUS_TTS_COMMAND", "")
        self.tts = CommandTTS(tts_cmd) if (tts_kind == "command" and tts_cmd) else MockTTS()
        player_cmd = os.environ.get("NEXUS_PLAYER_COMMAND", "")
        self.player = CommandPlayer(player_cmd) if player_cmd else MockPlayer()
        try:
            import sounddevice  # noqa: F401  (optional dep; validated here)
            self.mic: SoundDeviceMic | None = SoundDeviceMic()
        except ImportError:
            self.mic = None
            log.info("sounddevice not installed — microphone capture unavailable")
        self.voice = VoicePipeline(self.stt, self.tts, self.player, self.mic)
        log.info("voice: stt=%s tts=%s player=%s mic=%s",
                 self.stt.name, self.tts.name, self.player.name,
                 "sounddevice" if self.mic else "none")

    def _reload_router(self):
        providers = load_providers(self.store.conn, self.settings, self.secrets)
        self.router = ModelRouter(
            providers, offline_only=self.settings.offline_only,
            timeout_s=self.settings.request_timeout_s,
            max_retries=self.settings.max_retries)

    def new_planner(self) -> Planner:
        return Planner(self.router, self.registry,
                       max_iterations=self.settings.max_iterations,
                       task_timeout_s=self.settings.task_timeout_s)

    # -- chat --
    def _begin_turn(self, message: str, conversation_id: str | None = None):
        """Create conversation + task rows. Returns (cid, task_id, history)."""
        with self._lock:
            cid = conversation_id or self.memory.create_conversation(
                title=message[:60])
            self.memory.add_message(cid, Role.USER, message)
            history = self.memory.get_messages(cid, limit=40)[:-1]
            task_id = uuid.uuid4().hex[:12]
            self.store.conn.execute(
                "INSERT INTO tasks(id, conversation_id, title, status)"
                " VALUES (?,?,?,?)",
                (task_id, cid, message[:120], TaskStatus.RUNNING.value))
            self.store.conn.commit()
        return cid, task_id, history

    def _fail_turn(self, cid: str, task_id: str, events: list,
                 error: str) -> None:
        """Record a turn that died to an exception (planner blew up)."""
        with self._lock:
            self.store.conn.execute(
                "UPDATE tasks SET status=?, plan_json=?, updated_at=? WHERE id=?",
                (TaskStatus.FAILED.value, json.dumps({"error": error}),
                 datetime.now(timezone.utc).isoformat(), task_id))
            self.memory.add_message(
                cid, Role.ASSISTANT,
                f"Sorry — the task failed with an error: {error}")
            self.store.conn.commit()

    def _finish_turn(self, cid: str, task_id: str, events: list):
        """Persist assistant reply + task row. Returns (reply, final_payload)."""
        with self._lock:
            final = events[-1].payload if events else {}
            steps = final.get("steps", [])
            status = final.get("status", TaskStatus.DONE.value)
            self.store.conn.execute(
                "UPDATE tasks SET status=?, plan_json=?, updated_at=? WHERE id=?",
                (status, json.dumps(steps),
                 datetime.now(timezone.utc).isoformat(), task_id))
            reply_parts = [e.payload.get("text", "") for e in events
                           if e.kind == "message"]
            summary = final.get("summary", "")
            reply = "\n".join(p for p in reply_parts if p) or summary
            self.memory.add_message(cid, Role.ASSISTANT, reply or summary)
            self.store.conn.commit()
        if status in (TaskStatus.FAILED.value, TaskStatus.AWAITING_INPUT.value,
                      TaskStatus.AWAITING_APPROVAL.value):
            self.companion.notify(
                "task_failed" if status == TaskStatus.FAILED.value else status,
                f"Task {status.replace('_', ' ')}",
                (summary or "")[:200], {"task_id": task_id})
        return reply, final

    def chat_turn(self, message: str, conversation_id: str | None = None,
                  require_local: bool = False):
        """Non-interactive turn (elevated tools default-deny).
        Returns (conversation_id, events)."""
        cid, task_id, history = self._begin_turn(message, conversation_id)
        planner = self.new_planner()
        gen = planner.run(message, history=history,
                          approver=self._deny_elevated,
                          require_local=require_local, task_id=task_id)
        try:
            events = self._drain(gen)
        except Exception as e:
            self._fail_turn(cid, task_id, [], str(e))
            raise
        self._finish_turn(cid, task_id, events)
        return cid, events

    def _drain(self, gen):
        """Advance a planner generator to completion (per-step locking)."""
        events = []
        while True:
            with self._lock:
                try:
                    ev = next(gen)
                except StopIteration:
                    break
            events.append(ev)
        return events

    # -- interactive approval: suspend / resume --
    def _prune_waiting(self) -> None:
        """Drop approval-waits older than their timeout; mark those tasks
        cancelled so they never sit in 'awaiting_approval' unresumable."""
        with self._lock:
            now = time.monotonic()
            stale = []
            for tid, (ts, planner, _gen, _cid, _events) in self._waiting.items():
                if now - ts > planner.approval_timeout_s:
                    stale.append(tid)
            for tid in stale:
                del self._waiting[tid]
                self.store.conn.execute(
                    "UPDATE tasks SET status=?, plan_json=?, updated_at=?"
                    " WHERE id=?",
                    (TaskStatus.CANCELLED.value,
                     json.dumps({"note": "approval wait expired"}),
                     datetime.now(timezone.utc).isoformat(), tid))
            if stale:
                self.store.conn.commit()
                log.info("expired %d abandoned approval wait(s)", len(stale))

    def _suspend(self, task_id: str, planner: Planner, gen, cid: str,
                 events: list) -> None:
        """Park a planner paused at an approval_request."""
        self._prune_waiting()
        with self._lock:
            self._waiting[task_id] = (time.monotonic(), planner, gen, cid, events)
            # Make the pending approval visible to the companion app.
            req = next((e for e in events if e.kind == "approval_request"), None)
            pending = req.payload if req else {}
            self.store.conn.execute(
                "UPDATE tasks SET status=?, plan_json=?, updated_at=?"
                " WHERE id=?",
                (TaskStatus.AWAITING_APPROVAL.value,
                 json.dumps({"pending_approval": pending}),
                 datetime.now(timezone.utc).isoformat(), task_id))
            self.store.conn.commit()
        self.companion.notify(
            "approval",
            f"Approval needed: {pending.get('tool', 'a tool')}",
            f"Task {task_id} is waiting for your decision.",
            {"task_id": task_id, "tool": pending.get("tool"),
             "arguments": pending.get("arguments")})

    def _resume(self, task_id: str, approved: bool, by: str | None):
        """Pop a suspended turn and deliver the approval decision.
        Returns (planner, gen, cid, events) or None."""
        self._prune_waiting()
        with self._lock:
            item = self._waiting.pop(task_id, None)
        if item is None:
            return None
        _, planner, gen, cid, events = item
        planner.submit_approval(approved, by)
        return planner, gen, cid, events

    def _background_drain(self, planner: Planner, gen, task_id: str,
                          cid: str, events: list) -> None:
        """Keep draining a turn after the HTTP client went away, so the
        task can't get stuck at 'running'."""

        def _go():
            try:
                while True:
                    with self._lock:
                        try:
                            ev = next(gen)
                        except StopIteration:
                            break
                    events.append(ev)
                    if ev.kind == "approval_request":
                        self._suspend(task_id, planner, gen, cid, events)
                        return
                self._finish_turn(cid, task_id, events)
            except Exception as e:
                log.exception("background drain of task %s failed", task_id)
                try:
                    self._fail_turn(cid, task_id, events, str(e))
                except Exception:
                    log.exception("failed to record failure for task %s",
                                  task_id)

        threading.Thread(target=_go, daemon=True,
                         name=f"drain-{task_id}").start()

    def companion_resume(self, task_id: str, approved: bool,
                         by: str | None) -> bool:
        """Resume a suspended turn for a companion decision.

        The remainder of the turn drains in a background thread; the
        companion polls /companion/status for the outcome.
        """
        resumed = self._resume(task_id, approved, by)
        if resumed is None:
            return False
        planner, gen, cid, events = resumed
        self._background_drain(planner, gen, task_id, cid, events)
        return True

    def _cancel_task(self, task_id: str) -> bool:
        """Cancel a suspended (or recorded) task. Returns True if found."""
        with self._lock:
            item = self._waiting.pop(task_id, None)
            if item is not None:
                _, planner, _, _, _ = item
                planner.cancel()
            cur = self.store.conn.execute(
                "UPDATE tasks SET status=?, updated_at=? WHERE id=?",
                (TaskStatus.CANCELLED.value,
                 datetime.now(timezone.utc).isoformat(), task_id))
            self.store.conn.commit()
            return cur.rowcount > 0 or item is not None

    @staticmethod
    def _deny_elevated(tool_name, args) -> bool:
        log.warning("elevated tool '%s' denied (no approver in service mode)", tool_name)
        return False


class Handler(BaseHTTPRequestHandler):
    service: NexusService  # set by serve()

    # -- helpers --
    def _send(self, code: int, obj, content_type="application/json"):
        body = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _ok(self, obj):
        self._send(200, obj)

    def _err(self, code: int, message: str):
        self._send(code, {"error": message})

    # Hardening: bound request bodies (loopback-only service, but a buggy
    # client shouldn't be able to exhaust memory).
    MAX_JSON_BODY = 10 * 1024 * 1024
    MAX_AUDIO_BODY = 100 * 1024 * 1024

    def _read_limited(self, limit: int) -> bytes | None:
        """Read the request body up to `limit` bytes. None if over limit."""
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            length = 0
        if not length:
            return b""
        if length > limit:
            return None
        return self.rfile.read(length)

    def _body(self) -> dict:
        raw = self._read_limited(self.MAX_JSON_BODY)
        if raw is None:
            raise BodyTooLarge(
                f"JSON body exceeds {self.MAX_JSON_BODY} bytes")
        if not raw:
            return {}
        try:
            return json.loads(raw.decode())
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    def log_message(self, fmt, *args):  # route into our logger
        log.info("%s %s", self.command, self.path)

    def _sse(self, event, cid: str) -> bytes:
        chunk = json.dumps({"kind": event.kind, "payload": event.payload,
                            "conversation_id": cid})
        return f"data: {chunk}\n\n".encode()

    def _stream_gen(self, gen, planner, task_id: str, cid: str,
                    events: list) -> bool:
        """Drain a planner generator as SSE. Returns True if suspended
        at an approval_request (generator parked for /tasks/{id}/approve)."""
        while True:
            with self.service._lock:
                try:
                    ev = next(gen)
                except StopIteration:
                    return False
            if ev.kind == "approval_request":
                # Park BEFORE the client can see it: otherwise a fast
                # client could POST /approve before the entry exists.
                self.service._suspend(task_id, planner, gen, cid, events)
            events.append(ev)
            self.wfile.write(self._sse(ev, cid))
            self.wfile.flush()
            if ev.kind == "approval_request":
                return True

    def _companion(self):
        """Authenticate a companion request via Bearer device token.
        Returns the device dict, or sends 401 and returns None."""
        auth = self.headers.get("Authorization", "")
        token = auth[7:] if auth.startswith("Bearer ") else None
        device = self.service.companion.authenticate(token)
        if device is None:
            self._err(401, "invalid or missing device token")
            return None
        return device

    def _pending_approvals(self) -> list[dict]:
        with self.service._lock:
            rows = self.service.store.conn.execute(
                "SELECT id, title, plan_json, updated_at FROM tasks"
                " WHERE status='awaiting_approval' ORDER BY updated_at DESC"
                " LIMIT 50").fetchall()
            rows = [dict(r) for r in rows]
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["pending_approval"] = json.loads(
                    d.pop("plan_json") or "{}").get("pending_approval", {})
            except (ValueError, AttributeError):
                d["pending_approval"] = {}
            out.append(d)
        return out

    # -- routing --
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path, qs = parsed.path, urllib.parse.parse_qs(parsed.query)
        try:
            if path == "/health":
                self._ok({"ok": True, "version": __version__,
                          "providers": [
                              {"name": p.name, "local": p.is_local,
                               **p.health_check().__dict__}
                              for p in self.service.router.providers]})
            elif path == "/conversations":
                with self.service._lock:
                    rows = self.service.store.conn.execute(
                        "SELECT id, title, created_at, updated_at FROM conversations"
                        " ORDER BY updated_at DESC LIMIT 100").fetchall()
                self._ok({"conversations": [dict(r) for r in rows]})
            elif path == "/conversations/search":
                q = qs.get("q", [""])[0]
                try:
                    limit = max(1, min(int(qs.get("limit", ["10"])[0]), 100))
                except ValueError:
                    limit = 10
                if not q:
                    return self._err(400, "q is required")
                with self.service._lock:
                    hits = self.service.memory.search_messages(q, limit=limit)
                self._ok({"hits": hits})
            elif m := re.fullmatch(r"/conversations/([^/]+)/messages", path):
                with self.service._lock:
                    msgs = self.service.memory.get_messages(m.group(1))
                self._ok({"messages": [
                    {"role": m.role.value, "content": m.content} for m in msgs]})
            elif path == "/conversations/search":
                q = qs.get("q", [""])[0]
                if not q.strip():
                    return self._err(400, "q is required")
                try:
                    limit = max(1, min(int(qs.get("limit", ["10"])[0]), 50))
                except ValueError:
                    return self._err(400, "limit must be an integer")
                with self.service._lock:
                    hits = self.service.memory.search_messages(q, limit)
                self._ok({"hits": hits})
            elif path == "/providers":
                with self.service._lock:
                    rows = self.service.store.conn.execute(
                        "SELECT id, kind, base_url, model_id, is_local, enabled"
                        " FROM providers").fetchall()
                self._ok({"providers": [dict(r) for r in rows]})
            elif path == "/memories/search":
                q = qs.get("q", [""])[0]
                mode = qs.get("mode", ["hybrid"])[0]
                kind = qs.get("kind", [None])[0]
                project = qs.get("project_id", [None])[0]
                with self.service._lock:
                    if mode == "semantic":
                        mems = self.service.memory.recall_semantic(
                            q, self.service.embedder) if self.service.embedder else []
                    elif mode == "keyword":
                        mems = self.service.memory.recall(q, kind=kind)
                    else:
                        mems = self.service.memory.recall_hybrid(
                            q, self.service.embedder, kind=kind, project_id=project)
                self._ok({"memories": mems, "mode": mode})
            elif path == "/memories/pending":
                with self.service._lock:
                    pend = self.service.memory.list_pending()
                self._ok({"pending": pend})
            elif path == "/export":
                with self.service._lock:
                    data = self.service.memory.export_user_data()
                self._ok(data)
            elif path == "/voice/status":
                self._ok({"stt": self.service.stt.name,
                          "tts": self.service.tts.name,
                          "player": self.service.player.name,
                          "mic": "sounddevice" if self.service.mic else "none"})
            elif path == "/activity":
                try:
                    limit = max(1, min(int(qs.get("limit", ["50"])[0]), 200))
                except ValueError:
                    limit = 50
                with self.service._lock:
                    tools = self.service.store.conn.execute(
                        "SELECT id, task_id, tool_name, permission, status,"
                        " duration_ms, created_at FROM tool_executions"
                        " ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
                    audit = self.service.store.conn.execute(
                        "SELECT id, category, summary, created_at FROM audit_events"
                        " ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
                self._ok({"tool_executions": [dict(r) for r in tools],
                          "audit": [dict(r) for r in audit]})
            elif path == "/permissions":
                with self.service._lock:
                    rows = self.service.store.conn.execute(
                        "SELECT tool_name, level FROM permission_policies"
                        " ORDER BY tool_name").fetchall()
                self._ok({"policies": [dict(r) for r in rows]})
            elif path == "/tasks":
                try:
                    limit = max(1, min(int(qs.get("limit", ["30"])[0]), 200))
                except ValueError:
                    limit = 30
                with self.service._lock:
                    rows = self.service.store.conn.execute(
                        "SELECT id, conversation_id, title, status, updated_at"
                        " FROM tasks ORDER BY updated_at DESC LIMIT ?",
                        (limit,)).fetchall()
                self._ok({"tasks": [dict(r) for r in rows]})
            elif m := re.fullmatch(r"/tasks/([^/]+)", path):
                tid = m.group(1)
                with self.service._lock:
                    row = self.service.store.conn.execute(
                        "SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
                    tools = self.service.store.conn.execute(
                        "SELECT tool_name, permission, status, duration_ms,"
                        " created_at FROM tool_executions WHERE task_id=?"
                        " ORDER BY id", (tid,)).fetchall()
                if row is None:
                    return self._err(404, "unknown task")
                d = dict(row)
                d["tool_executions"] = [dict(r) for r in tools]
                self._ok({"task": d})
            elif path == "/workflows":
                with self.service._lock:
                    wfs = self.service.workflows.list_workflows()
                self._ok({"workflows": wfs})
            elif m := re.fullmatch(r"/workflows/([^/]+)", path):
                try:
                    with self.service._lock:
                        wf = self.service.workflows.get_workflow(m.group(1))
                except NexusError as e:
                    return self._err(404, f"{type(e).__name__}: {e}")
                self._ok({"workflow": wf})
            elif m := re.fullmatch(r"/workflow-runs/([^/]+)", path):
                try:
                    with self.service._lock:
                        run = self.service.workflows.get_run(m.group(1))
                except NexusError as e:
                    return self._err(404, f"{type(e).__name__}: {e}")
                self._ok({"run": run})
            elif path == "/dev/index":
                root = qs.get("root", [""])[0]
                try:
                    with self.service._lock:
                        st = self.service.dev.indexer.status(root or "~")
                except NexusError as e:
                    return self._err(400, f"{type(e).__name__}: {e}")
                self._ok(st)
            elif path == "/dev/patches":
                with self.service._lock:
                    patches = self.service.dev.patcher.list()
                self._ok({"patches": patches})
            elif m := re.fullmatch(r"/dev/patches/([^/]+)", path):
                try:
                    with self.service._lock:
                        p = self.service.dev.patcher.get(m.group(1))
                except NexusError as e:
                    return self._err(404, f"{type(e).__name__}: {e}")
                self._ok({"patch": p})
            elif path == "/companion/status":
                device = self._companion()
                if device is None:
                    return
                self._ok({"device": {"id": device["id"], "name": device["name"]},
                          "pending_approvals": self._pending_approvals(),
                          "unread_notifications":
                              self.service.companion.unread_count()})
            elif path == "/companion/approvals":
                device = self._companion()
                if device is None:
                    return
                self._ok({"approvals": self._pending_approvals()})
            elif path == "/companion/notifications":
                device = self._companion()
                if device is None:
                    return
                try:
                    since = int(qs.get("since", ["0"])[0])
                except ValueError:
                    since = 0
                self._ok({"notifications":
                          self.service.companion.list_notifications(since)})
            elif path == "/devices":
                with self.service._lock:
                    rows = self.service.store.conn.execute(
                        "SELECT id, name, created_at, last_seen FROM devices"
                        " ORDER BY created_at DESC").fetchall()
                self._ok({"devices": [dict(r) for r in rows]})
            elif path == "/backups":
                from .backup import list_backups as _list
                self._ok({"backups": _list(self.service.settings.data_dir)})
            else:
                self._err(404, "not found")
        except NexusError as e:
            self._err(400, f"{type(e).__name__}: {e}")
        except Exception as e:  # never leak tracebacks to clients
            log.exception("GET %s failed", path)
            self._err(500, "internal error")

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        try:
            if path == "/chat":
                body = self._body()
                if not body.get("message"):
                    return self._err(400, "message is required")
                cid, events = self.service.chat_turn(
                    body["message"], body.get("conversation_id"),
                    require_local=bool(body.get("require_local")))
                final = events[-1].payload if events else {}
                self._ok({"conversation_id": cid,
                          "status": final.get("status"),
                          "reply": final.get("summary", ""),
                          "events": [{"kind": e.kind, "payload": e.payload}
                                     for e in events]})
            elif path == "/chat/stream":
                body = self._body()
                if not body.get("message"):
                    return self._err(400, "message is required")
                interactive = bool(body.get("interactive", True))
                cid, task_id, history = self.service._begin_turn(
                    body["message"], body.get("conversation_id"))
                planner = self.service.new_planner()
                gen = planner.run(
                    body["message"], history=history,
                    approver=self.service._deny_elevated,
                    require_local=bool(body.get("require_local")),
                    task_id=task_id, interactive_approval=interactive)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                events: list = []
                try:
                    suspended = self._stream_gen(
                        gen, planner, task_id, cid, events)
                    if not suspended:
                        self.service._finish_turn(cid, task_id, events)
                        self.wfile.write(b"data: [DONE]\n\n")
                except (BrokenPipeError, ConnectionResetError):
                    # Client went away mid-turn. If the turn parked for
                    # approval it's safe; otherwise keep draining in
                    # background so the task can't strand at 'running'.
                    if task_id not in self.service._waiting:
                        self.service._background_drain(
                            planner, gen, task_id, cid, events)
                except NexusError as e:
                    # Planner died mid-turn (e.g. budget exceeded): don't
                    # leave the task stuck at 'running'; tell the client.
                    self.service._fail_turn(cid, task_id, events, str(e))
                    try:
                        err = json.dumps(
                            {"kind": "error", "payload": {"error": str(e)},
                             "conversation_id": cid})
                        self.wfile.write(f"data: {err}\n\n".encode())
                        self.wfile.write(b"data: [DONE]\n\n")
                    except (BrokenPipeError, ConnectionResetError):
                        pass
            elif path == "/conversations":
                body = self._body()
                cid = self.service.memory.create_conversation(body.get("title", ""))
                self._ok({"id": cid})
            elif path == "/providers":
                body = self._body()
                for f_ in ("id", "kind", "base_url", "model_id"):
                    if not body.get(f_):
                        return self._err(400, f"{f_} is required")
                if body["kind"] not in ("openai_compatible", "ollama", "mock"):
                    return self._err(400, "kind must be openai_compatible|ollama|mock")
                with self.service._lock:
                    self.service.store.conn.execute(
                        """INSERT INTO providers(id, kind, base_url, model_id, is_local)
                           VALUES (?,?,?,?,?)
                           ON CONFLICT(id) DO UPDATE SET
                             kind=excluded.kind, base_url=excluded.base_url,
                             model_id=excluded.model_id, is_local=excluded.is_local,
                             enabled=1""",
                        (body["id"], body["kind"], body["base_url"],
                         body["model_id"], int(bool(body.get("is_local")))))
                    self.service.store.conn.commit()
                    self.service._reload_router()
                self._ok({"ok": True})
            elif m := re.fullmatch(r"/permissions/([^/]+)", path):
                body = self._body()
                level = (body.get("level") or "").lower()
                if level not in ("observe", "assist", "execute", "elevated"):
                    return self._err(400, "level must be observe|assist|execute|elevated")
                try:
                    with self.service._lock:
                        self.service.registry.set_policy(m.group(1), Permission(level))
                except NexusError as e:
                    return self._err(404, f"{type(e).__name__}: {e}")
                self._ok({"ok": True, "tool": m.group(1), "level": level})
            elif m := re.fullmatch(r"/tasks/([^/]+)/approve", path):
                body = self._body()
                tid = m.group(1)
                resumed = self.service._resume(
                    tid, bool(body.get("approved", False)),
                    body.get("by", "user"))
                if resumed is None:
                    return self._err(
                        404, "no suspended task with that id (unknown or expired)")
                planner, gen, cid, events = resumed
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                try:
                    suspended = self._stream_gen(gen, planner, tid, cid, events)
                    if not suspended:
                        self.service._finish_turn(cid, tid, events)
                        self.wfile.write(b"data: [DONE]\n\n")
                except (BrokenPipeError, ConnectionResetError):
                    if tid not in self.service._waiting:
                        self.service._background_drain(
                            planner, gen, tid, cid, events)
                except NexusError as e:
                    self.service._fail_turn(cid, tid, events, str(e))
                    try:
                        err = json.dumps(
                            {"kind": "error", "payload": {"error": str(e)},
                             "conversation_id": cid})
                        self.wfile.write(f"data: {err}\n\n".encode())
                        self.wfile.write(b"data: [DONE]\n\n")
                    except (BrokenPipeError, ConnectionResetError):
                        pass
            elif m := re.fullmatch(r"/tasks/([^/]+)/cancel", path):
                if self.service._cancel_task(m.group(1)):
                    self._ok({"ok": True})
                else:
                    self._err(404, "unknown task")
            elif path == "/workflows":
                body = self._body()
                try:
                    with self.service._lock:
                        if body.get("from_task"):
                            wf = self.service.workflows.record_from_task(
                                body["from_task"], body.get("name", ""),
                                body.get("description", ""))
                        else:
                            wf = self.service.workflows.create_workflow(
                                body.get("name", ""), body.get("description", ""),
                                body.get("steps", []))
                except NexusError as e:
                    return self._err(400, f"{type(e).__name__}: {e}")
                self._ok({"workflow": wf})
            elif m := re.fullmatch(r"/workflows/([^/]+)/replay", path):
                body = self._body()
                try:
                    with self.service._lock:
                        result = self.service.workflows.replay(
                            m.group(1),
                            approver=self.service._deny_elevated,
                            dry_run=bool(body.get("dry_run", False)),
                            checkpoint=bool(body.get("checkpoint", True)))
                except NexusError as e:
                    return self._err(400, f"{type(e).__name__}: {e}")
                self._ok(result)
            elif m := re.fullmatch(r"/workflow-runs/([^/]+)/revert", path):
                try:
                    with self.service._lock:
                        result = self.service.workflows.revert_run(m.group(1))
                except NexusError as e:
                    return self._err(400, f"{type(e).__name__}: {e}")
                self._ok(result)
            elif path == "/pairing/code":
                self._ok(self.service.companion.create_pairing_code())
            elif path == "/pairing/redeem":
                body = self._body()
                if not body.get("code"):
                    return self._err(400, "code is required")
                try:
                    result = self.service.companion.redeem(
                        body["code"], body.get("device_name", ""))
                except NexusError as e:
                    return self._err(400, f"{type(e).__name__}: {e}")
                self._ok(result)
            elif path == "/backup":
                body = self._body()
                from .backup import backup as _backup
                try:
                    with self.service._lock:
                        manifest = _backup(self.service.store.conn,
                                           self.service.settings.data_dir,
                                           body.get("label", ""))
                except NexusError as e:
                    return self._err(400, f"{type(e).__name__}: {e}")
                self._ok({"backup": manifest})
            elif path == "/restore":
                body = self._body()
                if not body.get("name"):
                    return self._err(400, "name is required")
                from .backup import restore as _restore
                try:
                    with self.service._lock:
                        result = _restore(self.service.store.conn,
                                          self.service.settings.data_dir,
                                          body["name"])
                except NexusError as e:
                    return self._err(400, f"{type(e).__name__}: {e}")
                self._ok(result)
            elif m := re.fullmatch(r"/companion/approvals/([^/]+)", path):
                device = self._companion()
                if device is None:
                    return
                body = self._body()
                ok = self.service.companion_resume(
                    m.group(1), bool(body.get("approved", False)),
                    device["name"])
                if not ok:
                    return self._err(404, "no suspended task with that id")
                self._ok({"ok": True})
            elif m := re.fullmatch(r"/companion/notifications/(\d+)/read", path):
                device = self._companion()
                if device is None:
                    return
                self.service.companion.mark_read(int(m.group(1)))
                self._ok({"ok": True})
            elif path == "/memories":
                body = self._body()
                if not body.get("content"):
                    return self._err(400, "content is required")
                with self.service._lock:
                    mid = self.service.memory.remember(
                        body.get("kind", "fact"), body["content"],
                        consent=body.get("consent", "explicit"))
                self._ok({"id": mid})
            elif m := re.fullmatch(r"/memories/([^/]+)/approve", path):
                with self.service._lock:
                    self.service.memory.approve(m.group(1))
                self._ok({"ok": True, "id": m.group(1)})
            elif path == "/voice/transcribe":
                wav_bytes = self._read_limited(self.MAX_AUDIO_BODY)
                if wav_bytes is None:
                    return self._err(413, "audio body too large")
                if not wav_bytes:
                    return self._err(400, "WAV audio bytes required in body")
                try:
                    tr = self.service.stt.transcribe(wav_bytes)
                except NexusError as e:
                    return self._err(502, f"STT failed: {e}")
                self._ok({"text": tr.text, "language": tr.language,
                          "confidence": tr.confidence})
            elif path == "/voice/synthesize":
                body = self._body()
                if not body.get("text"):
                    return self._err(400, "text is required")
                try:
                    wav = self.service.tts.synthesize(body["text"])
                except NexusError as e:
                    return self._err(502, f"TTS failed: {e}")
                self._send(200, wav, content_type="audio/wav")
            else:
                self._err(404, "not found")
        except BodyTooLarge as e:
            self._err(413, str(e))
        except NexusError as e:
            self._err(400, f"{type(e).__name__}: {e}")
        except Exception:
            log.exception("POST %s failed", path)
            self._err(500, "internal error")

    def do_PATCH(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        try:
            if m := re.fullmatch(r"/memories/([^/]+)", path):
                body = self._body()
                with self.service._lock:
                    mem = self.service.memory.update_memory(
                        m.group(1), content=body.get("content"),
                        kind=body.get("kind"))
                self._ok({"memory": mem})
            elif m := re.fullmatch(r"/workflows/([^/]+)", path):
                body = self._body()
                with self.service._lock:
                    wf = self.service.workflows.update_workflow(
                        m.group(1), name=body.get("name"),
                        description=body.get("description"),
                        steps=body.get("steps"))
                self._ok({"workflow": wf})
            else:
                self._err(404, "not found")
        except BodyTooLarge as e:
            self._err(413, str(e))
        except NexusError as e:
            self._err(400, f"{type(e).__name__}: {e}")
        except Exception:
            log.exception("PATCH %s failed", path)
            self._err(500, "internal error")

    def do_DELETE(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        try:
            if m := re.fullmatch(r"/conversations/([^/]+)", path):
                with self.service._lock:
                    self.service.store.conn.execute(
                        "DELETE FROM conversations WHERE id=?", (m.group(1),))
                    self.service.store.conn.commit()
                self._ok({"ok": True})
            elif m := re.fullmatch(r"/providers/([^/]+)", path):
                with self.service._lock:
                    self.service.store.conn.execute(
                        "DELETE FROM providers WHERE id=?", (m.group(1),))
                    self.service.store.conn.commit()
                    self.service._reload_router()
                self._ok({"ok": True})
            elif m := re.fullmatch(r"/memories/([^/]+)", path):
                with self.service._lock:
                    deleted = self.service.memory.forget(m.group(1))
                self._ok({"deleted": deleted})
            elif m := re.fullmatch(r"/workflows/([^/]+)", path):
                try:
                    with self.service._lock:
                        self.service.workflows.delete_workflow(m.group(1))
                except NexusError as e:
                    return self._err(404, f"{type(e).__name__}: {e}")
                self._ok({"ok": True})
            elif m := re.fullmatch(r"/devices/([^/]+)", path):
                try:
                    with self.service._lock:
                        self.service.companion.revoke(m.group(1))
                except NexusError as e:
                    return self._err(404, f"{type(e).__name__}: {e}")
                self._ok({"ok": True})
            else:
                self._err(404, "not found")
        except Exception:
            log.exception("DELETE %s failed", path)
            self._err(500, "internal error")

def serve(settings: Settings | None = None) -> ThreadingHTTPServer:
    settings = settings or Settings()
    service = NexusService(settings)
    Handler.service = service
    server = ThreadingHTTPServer((settings.host, settings.port), Handler)
    log.info("NEXUS agent listening on %s:%d", settings.host, settings.port)
    return server


if __name__ == "__main__":
    serve().serve_forever()
