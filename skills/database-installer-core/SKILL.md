---
name: database-installer-core
description: Install and manage a local SQLite memory database and optional V1.5 Milvus vector Add-on, including runtime preflight, isolated environments, connection profiles, scoped read-only search/context/memory/health access, restore/upgrade, Milvus status/connect/bootstrap, self-tests, status, backup, and safe uninstall. Use for database installation/runtime and retrieval-context work, including bounded Context Packs and read-only MCP tools; do not use for conversation cleaning, final-answer synthesis, prompt policy, or answer-quality governance.
---

# Database Installer Core

Database Installer Core V1 is an installation and runtime manager for an external memory foundation. Package version `0.6.0` uses vendored `memory_core.skill_api` `1.4.0`. It can make durable local storage available, but it does not make an AI model stronger by itself. Answer quality still depends on downstream cleaning, evidence, state, query strategy, and the consuming Skill.

## Hard boundaries

- SQLite canonical remains mandatory; V1.5 adds an optional derived Milvus vector index.
- One- and two-character Chinese queries use a restricted SQLite substring fallback, not an unrestricted LIKE scan. Workspace/project/domain/sensitivity/owner filters remain applied before results are returned.
- `self-test` is transient: it creates a temporary SQLite database, verifies the chain, and deletes all test data.
- `sample` is optional teaching/display material. It is not an installation prerequisite and is never mixed with `self-test` or real user data.
- The hidden `demo` command is a compatibility alias only. It runs a transient self-test and never promises retained Demo data.
- SQLite-only mode never installs Docker/Milvus. V1.5 local Milvus uses a fixed Compose template only after explicit confirmation and an available Docker engine; it never auto-installs Docker.
- Do not read `.env`/`.secrets`, scan the whole disk, print secret values, or modify production canonical databases, collections, aliases, or scheduled tasks.
- The Skill may import only the versioned `memory_core.skill_api` boundary. It must not import internal pipeline modules such as `step3`–`step7d` or production resource constants.
- All writes use `plan -> apply -> verify` and exact confirmation tokens. `uninstall` preserves data, profile, and backups by default.
- Conversation handoff `plan` is read-only. `handoff apply` requires an exact token for the current plan and uses a short-lived local grant. This is single-machine controlled authorization, not IAM or multi-Agent delegation.

## User paths

- `doctor`: OS/Python/SQLite/FTS5/directory/secret-reference presence and a non-executing runtime repair plan.
- `install plan/apply`: create a new empty SQLite canonical and connection profile.
- `connect plan/apply`: read-only validation of an existing SQLite canonical, then write only a local profile.
- `self-test`: temporary SQLite test; write, idempotent replay, read, FTS, profile export, and cleanup are verified.
- `sample plan/apply`: explicit teaching/display database under the root chosen by the user.
- `restore plan/apply`: verify a backup, create a pre-restore backup, stage and verify the SQLite database, then restore with rollback on failure.
- `upgrade plan/apply`: inspect applied/pending schema migrations, create a pre-upgrade backup, migrate a staging copy, verify, and atomically replace the canonical; downgrade is blocked.
- `status`: installation status and per-capability state.
- `backup create`: SQLite canonical plus profile backup under the configured workspace backup root.
- `start` / `stop`: explicitly return `not_applicable` because SQLite V1 has no Skill-owned daemon.
- `uninstall`: remove only runtime state; preserve canonical, profile, and backups.
- `profile export`: export a profile without secret values.
- `search` / `context`: return scoped ranked hits or context only; the caller's LLM owns synthesis and final answer wording.
- `get-memory`: return one scoped active memory with citation metadata; evidence quotes require an explicit flag.
- `health`: run scoped read-only canonical, FTS, evidence, assertion, and Outbox diagnostics.
- `context-pack`: build a 3,500-token default Context Pack with current, conflict, history, source, and verification sections; `--deep` raises the explicit peak budget to 8,000.
- `mcp`: start a stdio MCP server exposing only `context`, `search`, `get_memory`, and `health`.
- `preset init` / `--preset`: generate and reuse a secret-free personal preset for repeated commands.
- `maintenance grants`: remove old expired or revoked grants after the configured retention window.
- `milvus sync`: process pending FTS/Milvus Outbox events through a configured non-production alias, with real or deterministic fake adapters.
- `handoff plan`: validate explicit handoff/approval artifacts, read canonical write idempotency mappings from the profile-owned SQLite DB, and produce read-only write/delete/blocked/pending counts without modifying canonical SQLite or Milvus.
- `handoff apply`: replan, verify `--confirm`, resolve the profile-owned SQLite canonical DB, and apply through the local ephemeral grant; confirmation or scope failures return `2`, item verification failures return `4`.

## V1.5 Milvus Add-on

Use one of three modes:

- SQLite-only: no Docker, Milvus, or Embedding dependency.
- Existing Milvus: `milvus status`, then `milvus connect plan/apply`; only a local Milvus profile is written, and the route must resolve to a test/staging alias.
- Local Milvus: `milvus install plan/apply` starts the fixed-version isolated Compose only after explicit confirmation; then `milvus bootstrap plan/apply` creates a test collection/alias and reconciles the index. The Compose ports bind to `127.0.0.1` only.
- Incremental sync: `milvus sync` processes pending FTS/Milvus Outbox events through the attached non-production profile. Use `--fake` for deterministic tests or configured env refs for real Embedding/Milvus.

`milvus bootstrap --backend fake` is the deterministic test path and does not call Docker, Milvus, or an Embedding API. `--backend real` uses `MILVUS_URI` and Embedding env references; values are never printed. Existing collections/aliases, `prod` names, and alias conflicts fail closed. Reconcile must reach zero differences before a vector bootstrap is reported successful.

The local Compose path requires `MILVUS_MINIO_USER` and `MILVUS_MINIO_PASSWORD` env refs and a working Docker engine. Docker/WSL2 installation, UAC, BIOS, security-software approval, and reboot remain user-controlled system actions.

Use `scripts/memory-db.cmd` (Windows) or `python scripts/db_skill.py` as the entry point.

For a runtime-only install, run `scripts/install_skill.ps1 -CodexHome <CODEX_HOME>`. The installer never copies `work`, `tests`, `.runtime`, user data, backups, `.env`, or `.secrets`. Remove runtime files with `scripts/uninstall_skill.ps1`; the uninstaller preserves unknown/user data. All commands support `--json`. Unavailable Milvus/legacy capabilities return exit code `2` with `blocked_capability`; start/stop instead return the truthful `not_applicable` state.

## Runtime repair

`scripts/runtime_preflight.py` is standard-library-only so it can diagnose missing Pydantic/memory_core even when normal imports fail. `scripts/bootstrap_runtime.ps1` is a plan-first helper:

- it detects Python 3.11–3.13;
- it prints commands without executing them unless the user explicitly passes `-Apply`;
- it creates `<workspace>/.runtime/venv` and installs exact versions from `assets/runtime/requirements-runtime.lock`;
- `-InstallPython` explicitly requests the controlled winget Python install, and `-Apply` explicitly executes the plan;
- after installation it re-probes Python; if PATH refresh, a new terminal, UAC, security software, or restart is required, it stops with the exact next step;
- it never installs Docker or modifies system Python.

## Capability states

- `sqlite`: `installed`, `missing`, `corrupt`
- `fts`: `available`, `unavailable`
- `vector`: `disabled`, `deferred`, `connected`, `degraded`
- V1.5 status also reports Docker/WSL2, collection, alias, embedding-presence, and reconcile readiness without printing secret values.
- `embedding`: `not_configured`, `configured_unverified`, `verified`

Missing Embedding configuration does not block SQLite installation. It is reported as `not_configured`; vector remains `disabled` or `deferred` rather than being described as indexed.

## Safety and data

- Managed profile paths are relative to `workspace_root`; an external absolute canonical path marks the profile `portable=false`.
- Profiles contain secret reference names only, never values.
- Known secret values, Authorization headers, credentialed URIs, and canaries block output. Ordinary hashes/UUIDs only produce a warning.
- Backups cover SQLite canonical and profile; Milvus metadata is not a V1 recovery source.
- Restore and forward-only upgrade are available for SQLite and require plan/apply/verify plus a pre-upgrade or pre-restore backup.
- SQLite V1 has no managed daemon; start/stop return `not_applicable` rather than pretending to control a process.
- Milvus operations are optional V1.5 capabilities; SQLite remains usable when vector or embedding is deferred.
- Production alias creation or switching is outside this Skill in all modes.

## References

- `references/scope-and-boundaries.md`
- `references/commands.md`
- `references/self-test-and-sample.md`
- `references/runtime-repair.md`
- `references/security.md`
- `references/install.md`
- `references/sqlite-query.md`
- `references/mcp.md`
- `scripts/Start-SQLite-Test.ps1` and `scripts/run_tests.ps1` for extracted-package self-test
- `references/milvus-addon.md`
- `README.md`
- `work/PHASE_D_REPORT.md`
- `work/PHASE_V15_REPORT.md`
- Extracted package test entry: `scripts/Start-SQLite-Test.ps1` or `scripts/run_tests.ps1`
- Plan: `../../architecture/DATABASE_SKILL_PLAN_V1.md`
