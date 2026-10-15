# NEXUS AI

A personal AI agent: voice + text assistant, computer automation, coding
helper, memory system, and local-AI companion. Original implementation —
inspired by the *capabilities* of assistants like Maya AI, built from scratch.

## Status (Phase 4 — Memory: complete)

Working and tested on Linux (Python 3.10+):

| Module | State |
|---|---|
| Provider abstraction (OpenAI-compatible / Ollama / mock) | ✅ implemented + tested |
| Provider SSE streaming (`stream_complete`, delta + tool-call reassembly) | ✅ implemented + tested |
| Provider factory (DB-configured gateways + env fallback, secrets via SecretStore) | ✅ implemented |
| Model router (failover, offline-only, retries) | ✅ implemented + tested |
| HTTP agent service on 127.0.0.1 (`/chat`, `/chat/stream` SSE, conversations, providers, memories, export) | ✅ implemented + tested |
| Voice (STT/TTS/mic/player interfaces, Whisper-compatible STT, command + mock TTS, push-to-talk, energy VAD, interruptible pipeline, `/voice/*` endpoints) | ✅ implemented + tested |
| Memory browser wiring (edit, inferred-pending review queue + approve, conversation search, semantic + hybrid recall) | ✅ implemented + tested |
| Computer tools (screen capture, OCR, active window, allowlisted app launch, gated mouse/keyboard, activity timeline, permission controls) | ✅ implemented + tested |
| Autonomous workflows (task records, observe-act-verify, retry budgets, interactive approval over SSE + resume, task history API) | ✅ implemented + tested |
| Workflow learning (record from task, editor, dry-run, safe replay with checkpoints + revert) | ✅ implemented + tested |
| Developer agent (project index + symbol search, patch propose/apply with diff preview, allowlisted test runs) | ✅ implemented + tested |
| Android companion (secure pairing, token auth, approvals + notifications API, Flutter scaffold) | ✅ implemented + tested |
| Production hardening (security review, body caps, backup/restore, user guide, packaging docs) | ✅ implemented + tested |
| SQLite schema + versioned migrations | ✅ implemented + tested |
| Memory service (CRUD, FTS search, consent gating, export/delete) | ✅ implemented + tested |
| Memory browser wiring (edit, pending-review queue, conversation search) | ✅ implemented + tested |
| Tool registry (schemas, 4 permission levels, audit log) | ✅ implemented + tested |
| Built-in tools (time, jailed file ops, allowlisted shell) | ✅ implemented + tested |
| Planner (bounded observe→act→verify loop, cancel, loop detection) | ✅ implemented + tested |
| Settings / logging / secret store (env + OS keyring) | ✅ implemented |
| Desktop shell (Tauri 2 + React scaffold) | 🟡 scaffolded, not built (needs Rust/Windows) |

**147/147 unit tests pass.** `python3 -m unittest discover -s tests` in `services/agent`.

## Quick start (agent core)

```bash
cd services/agent
python3 -m unittest discover -s tests   # run the suite
```

Try the loop end-to-end (mock provider, no API key needed):

```bash
NEXUS_DATA_DIR=/tmp/nexus-data python3 - <<'EOF'
# (see docs/setup.md for the full smoke-test script)
EOF
```

Copy `.env.example` to `.env` to configure a real provider.

## What's next

Phase 5 — Computer control: UI automation tools (screen/keyboard/mouse),
window inspection, safe action confirmations — elevated tools stay behind
the approval handshake.

Semantic retrieval note: Phase 4 evaluated embeddings and deliberately
stayed with FTS5/BM25 — the codebase is stdlib-only by design, and real
embeddings would need either a heavy dependency or a model download that
breaks the offline-first story. A pure-Python TF-IDF layer would only
duplicate what FTS5 already ranks well.

See `docs/architecture.md`, `docs/setup.md`, `docs/security.md`.

## Agent service (Phase 2)

```bash
cd services/agent
NEXUS_DATA_DIR=/tmp/nexus-data python3 -m nexus.service.app
# listening on 127.0.0.1:8765
```

Chat (persists the conversation):

```bash
curl -s -X POST localhost:8765/chat \
  -H 'Content-Type: application/json' \
  -d '{"message": "what time is it?"}' | python3 -m json.tool
```

Live activity stream (Server-Sent Events):

```bash
curl -N -X POST localhost:8765/chat/stream \
  -H 'Content-Type: application/json' \
  -d '{"message": "what time is it?"}'
```

Configure a custom gateway (secret comes from `NEXUS_<ID>_API_KEY` env var):

```bash
curl -s -X POST localhost:8765/providers \
  -H 'Content-Type: application/json' \
  -d '{"id": "my-gateway", "kind": "openai_compatible",
       "base_url": "https://gateway.example.com/v1", "model_id": "model-x"}'
```

## Voice (Phase 3)

```bash
# engine status
curl -s localhost:8765/voice/status

# transcribe a WAV file
curl -s -X POST localhost:8765/voice/transcribe \
  -H 'Content-Type: audio/wav' --data-binary @clip.wav

# synthesize speech -> speech.wav
curl -s -X POST localhost:8765/voice/synthesize \
  -H 'Content-Type: application/json' \
  -d '{"text": "Hello, I am NEXUS."}' -o speech.wav
```

## Computer tools (Phase 5)

```bash
# activity timeline (tool executions + audit)
curl -s "localhost:8765/activity?limit=20" | python3 -m json.tool

# task history + interactive approval
curl -s "localhost:8765/tasks?limit=10" | python3 -m json.tool
curl -s -X POST localhost:8765/tasks/<id>/approve \
  -H 'Content-Type: application/json' -d '{"approved": true}'

# record a workflow from a completed task, then replay it
curl -s -X POST localhost:8765/workflows \
  -H 'Content-Type: application/json' \
  -d '{"from_task": "<task-id>", "name": "morning routine"}'
curl -s -X POST localhost:8765/workflows/<id>/replay \
  -H 'Content-Type: application/json' -d '{"dry_run": true}'

# inspect and change tool permissions
curl -s localhost:8765/permissions | python3 -m json.tool
curl -s -X POST localhost:8765/permissions/click_at \
  -H 'Content-Type: application/json' -d '{"level": "execute"}'
```
