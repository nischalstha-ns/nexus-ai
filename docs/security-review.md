# NEXUS AI — Security Review (2026-10-15)

Review of the agent service as built. Method: code inspection + automated
sweeps, not vibes. Re-run the sweeps after any change to subprocess,
network, or auth code.

## Automated sweeps (all clean)

```bash
cd services/agent
grep -rn "shell=True" nexus/ --include="*.py" | grep -v tests   # -> none
grep -rn "pickle\.loads\|eval(\|exec(" nexus/ --include="*.py"    # -> none
grep -rn "0\.0\.0\.0" nexus/ --include="*.py"                    # -> none
```

- **No `shell=True` anywhere.** Every subprocess call uses an argv list.
  User-influenced commands go through allowlists:
  - `run_command` — first token must be in `NEXUS_ALLOWED_COMMANDS`
  - `open_app` — app name must be in `NEXUS_ALLOWED_APPS`
  - `run_tests` — command must be allowlisted
  - screen-capture/TTS/player commands are operator-configured env vars,
    never user input.
- **No pickle/eval/exec.** Tool arguments are JSON validated against
  schemas; no dynamic code execution from model output.
- **Loopback-only.** `NexusService` refuses to bind any non-loopback host
  (`127.0.0.1`, `localhost`, `::1` only). Remote companion access is
  documented via user-controlled tunnels (Tailscale, `adb reverse`).
- **Request body caps** (added in hardening): 10 MB JSON, 100 MB audio —
  a buggy client can't exhaust memory. Oversize → HTTP 413.
- **Tracebacks never leak**: all handlers catch `Exception` and return
  `{"error": "internal error"}`.

## Secrets

- Provider API keys live in environment variables or the OS keyring via
  `SecretStore` — never in SQLite, never in API responses
  (`GET /providers` excludes them), never in logs.
- Pairing codes: single-use, 10-minute TTL, sha256-hashed at rest.
- Device tokens: `secrets.token_urlsafe(32)`, sha256-hashed at rest,
  shown once.

## Permission model

| Level | Meaning |
|---|---|
| OBSERVE | read-only; always allowed |
| ASSIST | suggestion-only; never executes |
| EXECUTE | needs an explicit grant (policy table) |
| ELEVATED | always needs approval (interactive or pre-approved) |

- Input-injection tools (`click_at`, `type_text`, `press_keys`) default
  to ELEVATED. `ctrl+alt+delete` can never be sent (blocked in the key
  vocabulary).
- Policies are changeable at runtime via `POST /permissions/{tool}` and
  every change is audit-logged.
- Every tool execution is recorded in `tool_executions` + `audit_events`
  with permission level, approver, duration, and result.

## File access

- All file tools (`read_file`, `write_file`, `search_files`, dev tools,
  patcher) resolve paths against `NEXUS_APPROVED_ROOTS` and reject
  anything outside (`_resolve`).
- Patch application is all-or-nothing with stale-patch rejection:
  a patch whose `old_text` no longer matches exactly once is refused,
  never half-applied.

## Residual risks (honest)

1. **Real desktop backends are operator-configured.** `CommandScreenCap`,
   `CommandTTS`, `CommandPlayer` run operator-supplied commands — safe
   as configured, but review env vars on shared machines.
2. **The mock provider is a demo, not a model.** Point `NEXUS_PROVIDER`
   at a real gateway before trusting task outcomes.
3. **Companion tunnel is the user's.** The service never opens a LAN
   port; if the user exposes it another way, token auth is the only gate.
4. **No rate limiting on loopback.** Acceptable for a single-user local
   service; revisit if the bind policy ever changes.

## Full audit (2026-10-15, post-build)

- **Dead code sweep** (`services/agent/check_deadcode.py`, AST-based,
  whole-program): removed 10 unused imports across 8 modules. Remaining
  flags reviewed — all false positives (closures passed as tool handlers)
  or deliberate public API (`SecretStore.set_os_secret`,
  error taxonomy, `delete_user_data`).
- **Bugs found and fixed:**
  1. `recall_semantic` never backfilled embeddings in production — semantic
     search silently returned nothing. Now backfills lazily as documented.
  2. `score >= min_score` with `min_score=0.0` matched zero-similarity
     results. Now strictly greater — zero similarity never matches.
  3. Planner exceptions mid-stream left tasks stuck at `running` forever
     (streaming, approve-resume, non-interactive chat, companion background
     resume, workflow replay). All paths now record `failed` via
     `_fail_turn` and notify the client.
  4. Client disconnect mid-stream stranded the turn — now continues in a
     background drain (unless safely parked for approval).
  5. Abandoned approval waits leaked generators and left tasks in
     `awaiting_approval` unresumable — now pruned on suspend/resume and
     marked `cancelled`.
  6. `voice_turn` didn't pass `stop_event` to `listen` despite claiming
     every stage is interruptible.
  7. (Earlier) SQLite online backup hangs on uncommitted write
     transactions — commit-first in `backup()`/`restore()`.
- **Regression tests added** for: lazy embedding backfill, zero-similarity
  exclusion, planner-exception-marks-failed, oversized-body 413.
- **Final: 147/147 tests pass.**
