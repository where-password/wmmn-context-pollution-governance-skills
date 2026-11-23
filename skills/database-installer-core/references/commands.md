# Commands

Windows entry point: `scripts/memory-db.cmd`. Direct fallback: `python scripts/db_skill.py`. Package `0.6.0` vendors `memory_core.skill_api` `1.4.0`; all commands accept `--json`.

## Real user paths

- `doctor --workspace-root <PATH>`: read-only environment check plus a non-executing runtime repair plan.
- `install plan --workspace-root <PATH> --workspace-id <ID> --project-id <ID> --domain-profile <PROFILE> --owner-principal-id <OWNER> --plan-out <PLAN>`
- `install apply --plan-file <PLAN> --confirm-token <TOKEN>`
- `connect plan --workspace-root <PATH> --canonical-db <DB> --workspace-id <ID> --project-id <ID> --domain-profile <PROFILE> --owner-principal-id <OWNER> --plan-out <PLAN>`
- `connect apply --plan-file <PLAN> --confirm-token <TOKEN>`
- `self-test`: temporary SQLite test, automatic cleanup, no retained data.
- `restore plan --profile <PROFILE> --backup <BACKUP>`: verify and plan a restore.
- `restore apply --profile <PROFILE> --plan-file <PLAN> --confirm-token <TOKEN>`: stage, verify, restore, and rollback on failure.
- `upgrade plan --profile <PROFILE>`: inspect applied/pending schema migrations.
- `upgrade apply --profile <PROFILE> --plan-file <PLAN> --confirm-token <TOKEN>`: forward-only migration with pre-upgrade backup and rollback.
- `status --profile <PROFILE>`: installation and capability status.
- `profile export --profile <PROFILE> --output <PATH>`: profile without secret values.
- `backup create --profile <PROFILE> [--backup-dir <PATH>]`: SQLite canonical and profile backup.
- `start` / `stop`: explicitly `not_applicable` for SQLite-only V1; no fake daemon management.
- `uninstall --profile <PROFILE>`: runtime-only removal; preserve data, profile, and backups.

`sample plan/apply` is optional teaching/display material. It is not an installation prerequisite.

## Read-only retrieval and health

- `search --profile <PROFILE> --query <TEXT> [--project-id <ID>] [--top-k 10] [--max-sensitivity public|internal|private|secret] [--max-content-chars 2000] --json`: return `search_only` hits to the caller's LLM. Hits do not include internal `memory_id`.
- `context --profile <PROFILE> --query <TEXT> [--project-id <ID>] [--top-k 10] [--max-sensitivity <LEVEL>] [--max-content-chars 2000] --json`: return `context_only` retrieval context; no final answer is generated.
- `context-pack --profile <PROFILE> --query <TEXT> [--token-budget 3500] [--deep] [--project-id <ID>] [--max-sensitivity <LEVEL>] --json`: return `context_pack` with current, conflict, history, source, and verification sections. `--deep` sets 8,000 tokens.
- `mcp --profile <PROFILE> [--project-id <ID>]`: start the newline-delimited JSON-RPC MCP stdio server. Do not combine this command with human-readable output parsing.
- `get-memory --profile <PROFILE> --memory-id <ID> [--project-id <ID>] [--max-sensitivity <LEVEL>] [--max-content-chars 4000] [--include-evidence-quote] --json`: return one active memory in the requested scope. Evidence quotes are omitted by default.
- `health --profile <PROFILE> [--project-id <ID>] --json`: read-only schema, integrity, FTS, evidence, assertion, and Outbox checks.

The installer `ConnectionProfile` and an attached `skill-profile-v1` file are both accepted by these commands. The installer profile is adapted in memory; no second profile is written. Query results enforce workspace/project/domain, allowed visibility, maximum sensitivity, and private-owner scope before content is returned.

## Conversation handoff planning and apply

- `handoff plan --profile <PROFILE> --handoff-dir <DIR> [--written-memory-map <JSON>] --source-id <ID> [--project-id <ID>] [--conversation-id <ID>] --json`: read-only validation and planning for an explicit conversation handoff. The written map is resolved automatically from canonical SQLite; the flag is only a diagnostic or legacy override.
- The written-memory map accepts an explicit UTF-8 JSON file or an inline JSON object. Unknown fields and malformed values are blocked.
- The profile supplies workspace, domain/profile version, owner, and default project when exactly one project exists. Multi-project profiles require `--project-id`.
- Output includes handoff/plan IDs, scope, `writes`, `deletes`, `blocked_items`, `pending_post_write_revocations`, and `already_applied` counts plus a confirmation summary.
- The handoff directory must contain the five hashed data artifacts, including `source_units.jsonl`. Approved evidence must match a source unit’s source/workspace/project/scope/conversation, SHA-256 text hash, source metadata, visibility, sensitivity, and `text_content`.
- The plan command does not modify SQLite, canonical data, handoff artifacts, Milvus, or aliases.
- `handoff apply --profile <PROFILE> --handoff-dir <DIR> [--written-memory-map <JSON>] --source-id <ID> --confirm <TOKEN> [--project-id <ID>] [--conversation-id <ID>] [--principal-id <ID>] [--ttl-seconds 300] --json`: replan first and require exact `handoff-apply:<current-plan-id>` confirmation. Successful writes are discoverable from canonical SQLite for later supersede and revoke plans.
- Apply uses the canonical DB recorded by the profile. The profile supplies workspace/domain/profile version; project must be in profile scope; the principal defaults to the profile owner and an explicit override must match that owner.
- Apply registers or reuses required source/source_unit rows before memory writes. Existing row conflicts fail closed with zero memory writes; registration is idempotent and does not require manual source setup.
- A short-lived local grant is persisted for audit, restricted to the requested workspace/project/domain and actions `write`/`delete`, then revoked after the attempt. The HMAC secret is in-memory only and the token is never returned.
- This is single-machine controlled authorization, not IAM/OAuth, multi-Agent delegation, or a remote authentication system.
- Confirmation mismatch, invalid profile/scope, handoff/auth/config blocking return exit code `2`. An apply item/verification failure returns `4`.

## Milvus incremental sync

- `milvus sync --profile <PROFILE> [--milvus-profile <PATH>] [--fake] [--limit 100] [--owner <ID>] [--lease-seconds 60] --json`: process pending Outbox events into FTS and the configured Milvus write alias.
- Without `--milvus-profile`, the command expects `config/milvus-profile.json` under the connection profile workspace. A path to an attached `skill-profile-v1` Milvus profile is also accepted.
- Real mode requires `MILVUS_URI` and Embedding env refs. Use `--embedding-key-env`, `--embedding-base-url-env`, `--embedding-model-env`, and `--embedding-dim-env` to select alternate environment variable names.
- `--fake` uses in-memory FTS/vector adapters and does not call Docker, Milvus, or an external Embedding API.
- `completed` means no pending/claimed work remains after the batch; `nothing_to_do` means no events were pending; `partial` means a limit, retry, or poison state remains and exits `4`.
- Read/write aliases and the physical collection must all be non-production and resolve to the same collection. Supersede and revoke enqueue deletes for previously indexed records.

## Milvus V1.5 Add-on
- `milvus status --profile <PROFILE> [--route-file <ROUTE>] [--fake]`: vector/embedding/collection/alias/Docker/WSL2 status.
- `milvus connect plan --profile <PROFILE> --route-file <ROUTE> [--fake] --plan-out <PLAN>`: read-only existing Milvus attach plan.
- `milvus connect apply --plan-file <PLAN> --confirm-token <TOKEN>`: write only local Milvus profile.
- `milvus install plan --workspace-root <ROOT> --plan-out <PLAN>`: fixed Compose plan; never installs Docker.
- `milvus install apply --plan-file <PLAN> --confirm-token <TOKEN>`: explicit Compose startup with a working Docker engine.
- `milvus bootstrap plan --profile <PROFILE> --route-file <ROUTE> --backend fake|real --plan-out <PLAN>`: isolated collection/alias plan.
- `milvus bootstrap apply --profile <PROFILE> --plan-file <PLAN> --confirm-token <TOKEN> --backend fake|real`: create test resources and reconcile; fails closed on conflict.

`--backend fake` is deterministic and does not call Docker, Milvus, or an Embedding API. `real` requires a safe non-production target and env refs. `prod` aliases are rejected.

`demo` is hidden and compatibility-only. It runs a transient self-test and emits a warning; it does not promise a persistent Demo workspace.

## Registered capabilities

`start` and `stop` return an explicit `not_applicable` result for SQLite-only V1. `milvus` and legacy `init` remain registered but capability-blocked until their owning phases are implemented. `restore` and `upgrade` are available for SQLite.

## Personal preset and maintenance

- `preset init --profile <PROFILE> [--output <PATH>] [--force] --json`: write a secret-free preset under the profile workspace. Default output is `config/personal-preset.json`.
- `search|context|context-pack|get-memory|health|mcp --preset <PRESET>`: use preset profile/scope defaults when the corresponding flags are omitted.
- `handoff plan|apply --preset <PRESET> --handoff-dir <DIR> [--source-id <ID>]`: preset can supply profile, project, source, and conversation scope.
- `milvus sync --preset <PRESET> [--milvus-profile <PATH>]`: preset can supply the primary and attached Milvus profile paths.
- `maintenance grants --preset <PRESET> [--retention-days 7] --json`: remove expired/revoked grants after the retention window; active and recent audit records remain.

The final distribution excludes historical pipeline modules `step3`–`step7d`, `answer.py`, and `synthesis.py` from `vendor/memory_core`.

## Stable exits

- `0`: success
- `2`: blocked, unavailable capability, or dependency repair required
- `3`: confirmation required
- `4`: verification failure
- `5`: runtime/configuration error
