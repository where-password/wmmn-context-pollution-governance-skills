# Scope and Boundaries

V1 is `database-installer-core`: an external memory installation and runtime manager.

## In scope

- Runtime preflight and a non-executing repair plan.
- New empty SQLite canonical installation.
- Read-only connection validation for an existing SQLite canonical.
- Transient `self-test` with automatic cleanup.
- Optional, explicit `sample` for teaching/display.
- Status, profile export, SQLite backup, restore, forward-only upgrade, and runtime-only uninstall.
- Explicit `start/stop` `not_applicable` response for the no-daemon SQLite V1.
- Runtime-only Skill installation into a Codex home with safe uninstall entry.
- Optional V1.5 non-production Milvus status/connect, fixed local Compose plan, isolated test collection/alias, fake/real reconcile, and incremental Outbox sync.
- Read-only search/context/get-memory/health, bounded Context Packs, and the read-only MCP stdio surface.

## Out of scope

- Claiming that installing a database automatically improves AI answers.
- Conversation cleaning, candidate extraction, evidence policy, prompt policy, final-answer synthesis, or answer-quality scoring.
- Reading `.env`/`.secrets` or scanning the whole disk.
- Docker/Milvus installation in V1.
- Production collection/alias creation or any production alias switch; V1.5 only permits isolated test/staging resources.
- Persistent Demo as a default user mode.
