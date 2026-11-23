# database-installer-core

Local SQLite memory database installer and runtime manager for `database-installer-core-v1`.

Package version: `0.6.0`. Vendored `memory_core.skill_api`: `1.4.0`.

## What is included

- `SKILL.md`: assistant-facing entry point.
- `scripts/memory-db.cmd` / `scripts/db_skill.py`: Windows CLI.
- `self-test`: temporary SQLite end-to-end check; all test data is deleted.
- `handoff plan/apply`: read-only planning followed by explicitly confirmed local ingestion through a short-lived grant.
- `search/context/context-pack/get-memory/health`: scoped, read-only retrieval and diagnostics. These commands return memory context to the caller's LLM; they do not generate the final answer.
- `mcp`: read-only stdio MCP server exposing `context`, `search`, `get_memory`, and `health`.
- `preset init`: generate a secret-free personal preset for repeated local commands.
- `maintenance grants`: remove old expired/revoked local grants after a bounded audit-retention window.
- `install plan/apply`: create a new SQLite canonical and profile.
- `connect plan/apply`: validate and attach an existing SQLite canonical.
- `backup create`: SQLite canonical plus profile backup.
- `restore plan/apply`: verified backup restore with pre-restore backup and rollback.
- `upgrade plan/apply`: forward-only schema migration with pre-upgrade backup and rollback.
- `start` / `stop`: explicitly report `not_applicable` for the no-daemon SQLite V1.
- `scripts/Start-SQLite-Test.ps1`: package-local transient SQLite self-test.
- `scripts/run_tests.ps1`: package-local entry that invokes `Start-SQLite-Test.ps1`.
- `scripts/install_skill.ps1`: runtime-only installation into a Codex home.
- `scripts/uninstall_skill.ps1`: remove runtime files and vendor, preserve user data.
- `scripts/bootstrap_runtime.ps1`: plan-first Python/dependency repair; explicit `-InstallPython -Apply` is required to invoke winget.
- `milvus status/connect/install/bootstrap/sync`: optional V1.5 derived vector index and incremental Outbox synchronization.

SQLite-only mode does not require Docker, Milvus, or Embedding. V1.5 can attach an existing non-production Milvus or, only after explicit confirmation, start a fixed local Compose project with a working Docker engine. It never auto-installs Docker, creates production resources, or reads secret values.

## Quick start

Run from this package root:

`scripts\memory-db.cmd doctor --workspace-root <PATH> --json`

Run a transient self-test from the extracted package:

`scripts\Start-SQLite-Test.ps1 -Python <PYTHON_3.11_3.13>`

`scripts\run_tests.ps1 -Python <PYTHON_3.11_3.13>` is the package-local alias.

Create a real SQLite database:

`scripts\memory-db.cmd install plan --workspace-root <PATH> --workspace-id <ID> --project-id <ID> --domain-profile generic --owner-principal-id <OWNER> --plan-out <PLAN>`

Then apply the exact plan with its confirmation token. `sample` is optional teaching/display material and is not an installation prerequisite.

## Runtime repair

The Skill does not modify system Python. If Python or dependencies are missing, review the repair plan. A user who explicitly chooses the controlled installer path can run:

`scripts\bootstrap_runtime.ps1 -WorkspaceRoot <PATH> -InstallPython -Apply`

Use `-MemoryCoreSource` for a wheel or editable source. If Python needs a new terminal, PATH update, UAC approval, security-software approval, or restart, the script stops and reports the exact next step.

## Conversation handoff planning and apply

`memory-db handoff plan --profile <PROFILE> --handoff-dir <DIR> [--written-memory-map <JSON>] --source-id <ID> [--project-id <ID>] [--conversation-id <ID>] --json` validates the explicit handoff directory, data and approval artifact hashes, approval ledger, latest decisions, item hashes, and the canonical `idempotency_key -> memory_id/version` mapping through the vendored versioned `memory_core.skill_api`. The mapping is read automatically from the profile's canonical SQLite database; `--written-memory-map` is only an explicit diagnostic or legacy override.

The handoff directory must include the five hashed data artifacts, including `source_units.jsonl`. Plan validates each source unit’s source/workspace/project/scope/conversation identity, SHA-256 text hash, source metadata, visibility, sensitivity, and evidence quote coverage before producing a confirmation token.

The plan command is read-only. It returns the handoff/plan IDs, scope, counts, a confirmation summary, and `required_confirmation_token`. It does not create or update SQLite, canonical data, handoff artifacts, Milvus, or aliases.

Apply with `memory-db handoff apply --profile <PROFILE> --handoff-dir <DIR> [--written-memory-map <JSON>] --source-id <ID> --confirm <TOKEN> [--project-id <ID>] [--conversation-id <ID>] [--principal-id <ID>] [--ttl-seconds 300] --json`. The CLI replans first; the token must exactly match `handoff-apply:<current-plan-id>`. Any plan change invalidates an older token. The profile owns the canonical DB path; the default principal is the profile owner, and an explicit override must match that owner scope. Successful writes persist their mapping in canonical `idempotency_records`, so later supersede and revoke plans normally need no map file.

Apply authorizes and then registers or reuses every required source and source_unit in one fail-closed transaction before calling `MemoryWriter`. Registration is idempotent; an existing source or unit whose hash, text, scope, path, visibility, sensitivity, or metadata conflicts with `source_units.jsonl` aborts the apply with zero memory writes. Manual source pre-insertion is not required.

The database API creates a short-lived, SQLite-audited local grant limited to the requested workspace/project/domain and only `write`/`delete`, uses an in-memory HMAC secret for a one-call token, and revokes the grant after the attempt. This is single-machine controlled authorization, not IAM, OAuth, multi-Agent delegation, or a remote authentication service.

## Read-only retrieval and health

The installer connection profile can be passed directly to the retrieval commands. The Skill validates it, resolves the portable canonical SQLite path, maps capabilities, and adapts it to the query contract without writing a second profile file.

- `memory-db search --profile <PROFILE> --query <TEXT> [--project-id <ID>] [--top-k 10] [--max-sensitivity <LEVEL>] --json`: returns `search_only` ranked hits. Internal memory IDs are not included in hits.
- `memory-db context --profile <PROFILE> --query <TEXT> [--project-id <ID>] [--top-k 10] --json`: returns `context_only` retrieval context for the caller-owned LLM.
- `memory-db get-memory --profile <PROFILE> --memory-id <ID> [--include-evidence-quote] --json`: returns one scoped active memory and citation metadata. Evidence quotes are hidden unless explicitly requested.
- `memory-db health --profile <PROFILE> [--project-id <ID>] --json`: checks canonical integrity, schema migrations, FTS consistency, evidence, assertion states, and Outbox freshness without writes.

All commands enforce workspace, project, domain, sensitivity, visibility, and private-owner scope. `search` and `context` do not expose `memory_id`; `get-memory` requires an explicit ID and returns it because that ID was the request target. The Skill never calls a model to synthesize an answer in these interfaces. Ranked FTS search depends on the Outbox/FTS sync state; `health` reports pending or claimed Outbox rows as stale. Run `milvus sync` to process pending FTS and Milvus events incrementally.

## Personal preset and maintenance

`memory-db preset init --profile <CONNECTION_PROFILE> --json` writes `config/personal-preset.json` inside the profile workspace. The preset contains a relative profile path, default project, source id, sensitivity, and sync owner; it never contains API keys or tokens.

Personal commands can then omit the profile and repeated scope arguments:

`memory-db search --preset <PRESET> --query <TEXT> --json`

`memory-db handoff plan --preset <PRESET> --handoff-dir <DIR> --json`

`memory-db milvus sync --preset <PRESET> --fake --json`

`--preset` can also supply `handoff_dir`, `source_id`, `project_id`, `milvus_profile`, and `owner`. Explicit CLI values still win.

`memory-db maintenance grants --preset <PRESET> --retention-days 7 --json` removes expired/revoked grants only after the retention window. Recent audit records and active grants are preserved.

The vendored runtime is trimmed to the installation/query/MCP surface; historical `step3`–`step7d`, `answer.py`, and `synthesis.py` modules are not shipped.

## Context Pack and MCP

`memory-db context-pack --profile <PROFILE> --query <TEXT> --json` builds a bounded `context_pack` result. The default budget is 3,500 tokens; `--deep` selects the explicit 8,000-token peak budget.

The pack prioritizes current active memories, includes same-claim pending-review conflicts and superseded history when available, and carries citation, evidence, verification, sensitivity, and visibility metadata. Token fitting is deterministic and conservative. Critical current entries must fit or the request fails closed; lower-priority history is truncated or omitted first.

Start the MCP server with `memory-db mcp --profile <PROFILE>`. It uses newline-delimited JSON-RPC over stdio and exposes only four read-only tools:

- `context`: Context Pack for a query.
- `search`: ranked memory hits without internal memory IDs.
- `get_memory`: one scoped active memory; evidence quotes are hidden unless requested.
- `health`: canonical, FTS, evidence, assertion, and Outbox diagnostics.

The MCP server does not expose writes, installation, Milvus administration, or final-answer synthesis. For a Milvus-backed profile, search/context lazily use the configured read-only index and Embedding env refs; missing configuration returns a tool error without printing secrets.

## Incremental Milvus sync

After `milvus connect plan/apply`, run `memory-db milvus sync --profile <CONNECTION_PROFILE> --json`. The command resolves `config/milvus-profile.json` by default; `--milvus-profile <PATH>` can select another attached Milvus profile.

- `--fake` uses deterministic in-memory adapters and makes no Docker, Milvus, or Embedding network call.
- Real mode requires `MILVUS_URI` plus the configured Embedding env refs. Defaults are `EMBEDDING_API_KEY`, `EMBEDDING_BASE_URL`, `EMBEDDING_MODEL`, and `EMBEDDING_DIM`; values are never printed.
- The command requires the read/write aliases and physical collection to resolve to the same non-production target.
- Pending canonical Outbox events update FTS and the configured Milvus write alias.
- Supersede emits delete events for the replaced FTS/Milvus records; revoke emits delete events through the same worker path.
- Events are processed with version-aware, lease-based, idempotent retries. `partial` means a batch limit, retry, or poison state leaves work pending.
- Milvus search always rechecks canonical SQLite after vector retrieval, so superseded, revoked, out-of-scope, or non-active IDs are discarded even if the vector index is temporarily stale.

## Optional V1.5 Milvus

- Existing Milvus: `milvus status`, then `milvus connect plan/apply`.
- Local Milvus: `milvus install plan/apply`, then `milvus bootstrap plan/apply`.
- Deterministic testing: add `--backend fake` to bootstrap; this does not call Docker, Milvus, or an Embedding API.
- Reconcile must end with zero differences. `prod` aliases and existing resource conflicts fail closed.
- Local Compose requires `MILVUS_MINIO_USER`, `MILVUS_MINIO_PASSWORD`, and a working Docker engine. Real vector bootstrap additionally uses `MILVUS_URI` and Embedding env refs.

## Install into a Codex home

`scripts\install_skill.ps1 -CodexHome <CODEX_HOME>`

The default never overwrites an existing target and copies runtime files only. Tests, work reports, databases, secrets, `.env`, and user data are excluded. Uninstall with:

`scripts\uninstall_skill.ps1 -CodexHome <CODEX_HOME>`

Uninstall preserves user workspaces/runtime data, databases, backups, config, and unknown files.
