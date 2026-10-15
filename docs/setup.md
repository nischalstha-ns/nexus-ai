# NEXUS AI — Setup Guide

Everything you need to get NEXUS running from a fresh clone.

## 1. Requirements

| Component | Needs |
|---|---|
| Agent service (core) | Python 3.10+, SQLite 3.35+ (FTS5) — **no pip packages required** |
| Voice: real STT | An OpenAI-compatible `/audio/transcriptions` endpoint, or keep the mock |
| Voice: microphone | `pip install sounddevice` (optional) |
| Voice: speech out | `espeak-ng` (optional, via `NEXUS_TTS_COMMAND`) |
| Screen capture | `grim` (Wayland) / `screencapture` (macOS) / `tesseract-ocr` for OCR (all optional) |
| Desktop shell | Node 18+, Rust stable (scaffold only — see below) |
| Companion app | Flutter 3.x + Android SDK (scaffold only — see below) |

Check SQLite: `python3 -c "import sqlite3; print(sqlite3.sqlite_version)"`

## 2. Clone and verify

```bash
git clone https://github.com/nischalstha-ns/nexus-ai.git
cd nexus-ai/services/agent
python3 -m unittest discover -s tests
```

Expected: `Ran 147 tests ... OK`.

## 3. Configure

```bash
cp ../../.env.example ../../.env   # then edit it
```

The important variables:

```bash
NEXUS_DATA_DIR=~/nexus-data            # where the SQLite DB lives
NEXUS_APPROVED_ROOTS=~/projects        # colon-separated; files the agent may touch
NEXUS_ALLOWED_COMMANDS=ls,echo,python3,pytest,git
NEXUS_PROVIDER=mock                    # mock | openai | ollama | custom
NEXUS_OFFLINE_ONLY=1                   # refuse all cloud providers
```

For a real model (OpenAI-compatible):

```bash
NEXUS_PROVIDER=openai
NEXUS_OPENAI_BASE_URL=https://api.openai.com/v1
NEXUS_OPENAI_MODEL=gpt-4o-mini
NEXUS_OPENAI_API_KEY=<your-key>        # or store in OS keyring
```

For local models via Ollama: `NEXUS_PROVIDER=ollama`,
`NEXUS_OLLAMA_BASE_URL=http://127.0.0.1:11434/v1`.

## 4. Start the service

```bash
cd services/agent
python3 -m nexus.service.app
# NEXUS agent listening on 127.0.0.1:8765
```

The service **only binds loopback** — it refuses to listen on LAN addresses.
All 5 database migrations apply automatically on first start.

## 5. Try it

```bash
# health
curl -s localhost:8765/health

# chat (mock provider answers immediately; point NEXUS_PROVIDER at a
# real gateway for real intelligence)
curl -s -X POST localhost:8765/chat \
  -H 'Content-Type: application/json' \
  -d '{"message": "hello"}' | python3 -m json.tool

# live progress as Server-Sent Events
curl -N -X POST localhost:8765/chat/stream \
  -H 'Content-Type: application/json' \
  -d '{"message": "what time is it?"}'
```

Elevated tool calls pause the stream with an `approval_request` event —
approve with:

```bash
curl -s -X POST localhost:8765/tasks/<task-id>/approve \
  -H 'Content-Type: application/json' -d '{"approved": true}'
```

More in `docs/user-guide.md`: memory, workflows, developer tools,
computer control, backup/restore.

## 6. Voice (optional)

```bash
# real speech-to-text via an OpenAI-compatible endpoint
NEXUS_STT=openai
NEXUS_STT_URL=https://api.openai.com/v1
NEXUS_STT_API_KEY=<key>

# real text-to-speech via a command (e.g. espeak-ng)
NEXUS_TTS=command
NEXUS_TTS_COMMAND="espeak-ng --stdout"

# microphone (needs `pip install sounddevice`)
# push-to-talk is exposed through the /voice/* endpoints
```

Test: `curl -s localhost:8765/voice/status`

## 7. Computer control (optional)

```bash
# screen capture: point at a real tool (Wayland/macOS examples)
NEXUS_SCREEN=command
NEXUS_SCREENCAP_COMMAND="grim -"                 # Wayland
# NEXUS_SCREENCAP_COMMAND="screencapture -x -t png -"  # macOS

# OCR (needs tesseract-ocr installed)
NEXUS_OCR=tesseract

# apps the agent is allowed to launch (name=command pairs)
NEXUS_ALLOWED_APPS="browser=firefox;editor=code"
```

Input tools (`click_at`, `type_text`, `press_keys`) default to
ELEVATED — every use needs approval. Real mouse/keyboard backends are
platform-specific; the mock backend records actions for dry runs.

## 8. Android companion (optional)

The service never opens a LAN port. Reach it from the phone via:

```bash
adb reverse tcp:8765 tcp:8765        # then base URL http://127.0.0.1:8765
# or over Tailscale: http://<desktop-tailscale-ip>:8765
```

Pair:

```bash
curl -s -X POST localhost:8765/pairing/code   # 8-letter code, 10 min, single use
```

Build the app (`companion/`):

```bash
export PATH=~/workspace/sdks/flutter/bin:$PATH   # or your Flutter install
cd companion && flutter pub get && flutter build apk --release
```

## 9. Desktop shell (scaffold)

```bash
cd apps/desktop
npm install
npm run tauri dev     # needs Rust; builds the installer with `npx tauri build`
```

The shell talks to the agent service at `http://127.0.0.1:8765`.
Windows packaging notes: `packaging/windows-build.md`.

## 10. Backup

```bash
curl -s -X POST localhost:8765/backup \
  -H 'Content-Type: application/json' -d '{"label": "pre-upgrade"}'
curl -s localhost:8765/backups
# restore (no restart needed):
curl -s -X POST localhost:8765/restore \
  -H 'Content-Type: application/json' -d '{"name": "<backup-file>.db"}'
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| `no such table` | Migrations dir wrong — the service resolves `data/migrations` from the repo root automatically; don't move it |
| Provider auth errors | Fail fast by design — check `.env`, don't retry blindly |
| `tesseract is not installed` | `apt install tesseract-ocr` or keep `NEXUS_OCR=mock` |
| Stream hangs at approval | That's the pause — `POST /tasks/<id>/approve` to resume |
| Tests fail after pull | `git pull`, re-run `python3 -m unittest discover -s tests` in `services/agent` |
