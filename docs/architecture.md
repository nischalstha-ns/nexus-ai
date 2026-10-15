# NEXUS AI — Architecture

## Modules

```
apps/desktop        Tauri 2 + React + TS + Vite shell (Phase 1: scaffold)
services/agent      Python agent service (Phase 1: working core)
  nexus/core        types, errors, config, logging, secrets
  nexus/providers   AIProvider ABC; openai_compat (urllib, stdlib-only); mock
  nexus/router      ModelRouter: ordered failover, offline-only, bounded retries
  nexus/memory      Store (SQLite + migrations) + MemoryService
  nexus/tools       ToolRegistry (permissions/audit) + builtin tools
  nexus/planner     Planner: bounded observe→act→verify loop
packages/shared-types  (Phase 2+: TS types mirroring core/types.py)
data/migrations     versioned SQL, applied transactionally at startup
```

## Key design decisions

- **Stdlib-only Python in Phase 1.** No pip installs needed to run or test.
  Optional deps (keyring, Whisper, etc.) arrive with their phases.
- **The model never acts directly.** It proposes tool calls; the registry
  permission-gates, validates, and audits every execution.
- **Permissions:** observe (read-only) / assist (suggest-only, never executes) /
  execute (permitted low-risk) / elevated (requires approver callback).
- **Memory consent:** inferred facts are stored `inferred-pending` and are
  invisible to recall until the user approves them.
- **Planner bounds:** max iterations, wall-clock timeout, cancellation event,
  and loop detection (same tool+args 3× → stop).
- **Secrets:** env vars or OS keyring. Never in SQLite, logs, or git.
- **Local-only communication:** the agent service binds 127.0.0.1 only;
  no unauthenticated network endpoints.

## Data model

See `data/migrations/001_initial.sql`: conversations, messages (+FTS5),
memories (+FTS5), projects, tasks, tool_executions, permission_policies,
providers, user_settings, audit_events (append-only).
