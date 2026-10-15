# NEXUS AI — Security model

## Principles

1. **Least privilege.** Tools declare a permission level; the registry enforces it.
2. **Explicit confirmation for consequential actions.** Deleting files,
   installing software, sending messages, purchases, credential access —
   these are ELEVATED and require an approver callback (the UI in Phase 2).
3. **Jailed filesystem.** All file tools resolve paths against
   `NEXUS_APPROVED_ROOTS`; `..` escapes raise `PermissionError` (tested).
4. **Command allowlist.** `run_command` takes no shell; argv[0] must be in
   `NEXUS_ALLOWED_COMMANDS` (tested).
5. **No secret storage in the DB.** API keys live in env vars or the OS
   keyring via `SecretStore`. The `providers` table stores base URL + model
   ID only.
6. **Audit everything.** Every tool execution writes to `tool_executions`
   and the append-only `audit_events` table (tested).
7. **Untrusted external content.** Instructions found in web pages, docs, or
   tool output are data, never permission. The planner only executes tools
   the registry allows.
8. **Local-only service.** The agent binds 127.0.0.1; the desktop app talks
   to it over loopback. No unauthenticated network control endpoints.
9. **Bounded autonomy.** Max iterations, task timeout, cancellation, and
   loop detection are enforced in the planner (tested).

## What is NOT yet implemented (honest list)

- Voice (Phase 3), computer control/UI automation (Phase 5), workflow
  learning (Phase 7), Android companion (Phase 9).
- The desktop shell is scaffolded, not built (needs Rust).
- Embedding-based semantic memory (FTS5 keyword search is implemented).
