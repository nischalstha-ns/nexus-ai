# NEXUS AI — User Guide

## Start the service

```bash
cd ~/workspace/nexus-ai/services/agent
export NEXUS_DATA_DIR=~/nexus-data          # default: ./data
export NEXUS_APPROVED_ROOTS=~/projects       # files the agent may touch
python3 -m nexus.service.app                 # listens on 127.0.0.1:8765
```

Copy `.env.example` to `.env` for the full option list (providers, voice,
embeddings, screen capture, test command).

## Chat

```bash
curl -s -X POST localhost:8765/chat \
  -H 'Content-Type: application/json' \
  -d '{"message": "what can you do?"}' | python3 -m json.tool
```

For live progress, use `/chat/stream` (Server-Sent Events). Elevated tool
calls pause the stream with an `approval_request` event; approve with:

```bash
curl -s -X POST localhost:8765/tasks/<task-id>/approve \
  -H 'Content-Type: application/json' -d '{"approved": true}'
```

## Memory

- `GET /memories/search?q=&mode=hybrid|semantic|keyword`
- Inferred facts land in `GET /memories/pending` — approve with
  `POST /memories/{id}/approve`.
- `PATCH /memories/{id}` edits; `DELETE /memories/{id}` forgets.
- `GET /export` dumps everything as JSON.

## Computer control

```bash
curl -s "localhost:8765/activity?limit=20"   # what the agent did
curl -s localhost:8765/permissions           # tool permission levels
```

Screen/OCR backends: `NEXUS_SCREEN=command` +
`NEXUS_SCREENCAP_COMMAND="grim -"` (Wayland) or
`"screencapture -x -t png -"` (macOS); `NEXUS_OCR=tesseract`.

## Workflows (teach by showing)

1. Do a task once via chat.
2. Record it: `POST /workflows {"from_task": "<id>", "name": "..."}`
3. Edit steps: `PATCH /workflows/<id> {"steps": [...]}`
4. Dry-run: `POST /workflows/<id>/replay {"dry_run": true}`
5. Replay for real — files are checkpointed; revert with
   `POST /workflow-runs/<run-id>/revert`.

## Developer agent

```bash
# index a project, then ask via chat or tools:
# index_project -> find_symbol / search_code
# propose_patch (diff preview, writes nothing) -> apply_patch
# run_tests (allowlisted command, no shell)
```

## Android companion

See `companion/README.md`. Pair with `POST /pairing/code`, connect over
`adb reverse` or Tailscale, approve tasks from your phone.

## Backup

```bash
curl -s -X POST localhost:8765/backup -H 'Content-Type: application/json' \
  -d '{"label": "before-experiments"}'
curl -s localhost:8765/backups
curl -s -X POST localhost:8765/restore -H 'Content-Type: application/json' \
  -d '{"name": "nexus-20261015-090000-before-experiments.db"}'
# or offline: python3 -m nexus.service.backup backup --label x
```

## Performance (measured 2026-10-15, this machine)

- Service startup: ~0.01 s
- Mock chat turn: <0.01 s
- Mock screen capture: ~38 ms
- Indexing 40 files / 327 symbols: 0.10 s
