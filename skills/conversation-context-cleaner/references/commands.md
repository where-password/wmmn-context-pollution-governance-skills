# Commands

All commands emit JSON. Add `--json` for compact output; without it, the same object is pretty-printed.

## Doctor

`context-cleaner doctor --workspace-root <PATH> --json`

Add `--input-root <PATH> --input <FILE>` to check an explicit input path and scan its text for sensitive marker categories without retaining matched values.

## Plan

`context-cleaner clean plan ... --json`

`clean plan` is read-only. It does not create the workspace, state root, run directory, or plan file. Heuristic caps default to 30 per user message and 100 per run; override with `--heuristic-per-message-cap` and `--heuristic-total-cap`. Persist stdout yourself if the plan will be applied.

Windows PowerShell example:

`context-cleaner clean plan <ARGS> --json | Set-Content -LiteralPath <PLAN_JSON> -Encoding utf8NoBOM`

## Apply

`context-cleaner clean apply --plan <PLAN_JSON> --confirm <CONFIRMATION_TOKEN> --json`

Apply requires both the exact plan file and the exact confirmation token from that plan. It re-reads the input, re-parses it, rebuilds the extraction, and rejects the plan if the input SHA, extraction digest, plan ID, or confirmation token differs.

Statuses:

- `applied`: the run was newly written.
- `already_applied`: the run exists, matches the plan, and every artifact hash verifies; `changed=false`.
- `blocked`: no run was written or the existing run is incomplete.

## Status

`context-cleaner status --workspace-root <PATH> --json`

Reads only the isolated state root. It reports run and export counts, validates all artifacts listed in each run manifest, and returns `warning` for incomplete runs or unreadable export manifests.

## Review export

`context-cleaner review export --workspace-root <PATH> --run-id <RUN_ID> --json`

The default output is `<workspace-root>/conversation-cleaner/v1/exports/<run_id>/`. It includes raw candidates, consolidated groups, parent groups, task history, noise items, open questions, suppressed audit, review bundle/stats/Markdown, and the audit report. A custom `--output-dir` must remain inside the workspace root. Re-export to the same location is a verified no-op.

## Handoff export

`context-cleaner handoff export --workspace-root <ROOT> --run-id <RUN_ID> --observations-root <ROOT> --observations <FILE> --workspace-id <ID> --project-id <ID> --domain-profile <NAME> --profile-version <VERSION> --source-id <ID> --json`

The observations file is an explicit JSONL snapshot; blank lines, unknown fields, missing required fields, and malformed `MemoryObservation` records block the export. The observations file must remain under `--observations-root`, must not be a symlink, and is secret-scanned before parsing. No directory or glob discovery is performed.

The source run must already exist and its manifest and artifact hashes must validate. Every observation evidence `source_unit_id`, conversation, source sequence, and quoted character span must be compatible with that run's `messages.jsonl`.

The command resolves deterministic conversation state and builds database handoff suggestions. It does not write canonical memory, Milvus, aliases, promotions, or database records. The versioned `conversation_cleaner.api_v1.ConversationSkillApi` surface includes `handoff_export`; the overall API version is unchanged in this step.

Output is written under:

`<workspace-root>/conversation-cleaner/v1/handoffs/<handoff_id>/`

Artifacts:

- `memory_write_batch.jsonl`
- `state_transitions.jsonl`
- `review_pending.jsonl`
- `handoff_manifest.json`
- `HANDOFF_REVIEW.md`

`handoff_id` binds the run source SHA, observations SHA, handoff contract, workspace/project/domain/profile, and source ID. Repeating the exact same export returns `already_exported` with `changed=false`. Changed observations or scope produce a new handoff directory without overwriting an earlier one.

Exit codes:

- `0`: ok, warning, planned, applied, already applied, exported, already exported, handoff already exported, recorded, duplicate, generated, or already generated.
- `1`: runtime error.
- `2`: blocked operation or blocked plan.

## Handoff approval review

The versioned `ConversationSkillApi` surface also includes:

- `handoff_review_decide`
- `handoff_review_status`
- `handoff_review_export_approved`

The overall API version is frozen at `1.5.0`.

### Decide

`context-cleaner handoff review decide --workspace-root <ROOT> --handoff-id <HANDOFF_ID> --idempotency-key <KEY> --decision approve|reject|defer|revoke --reviewer-id <ID> --reason <TEXT> --reviewed-at <RFC3339> --json`

The handoff directory must be under `<workspace-root>/conversation-cleaner/v1/handoffs/<handoff_id>`. The item key must exist in that handoff's `memory_write_batch.jsonl`, and the item hash must match. Decisions are appended to `approval_ledger.jsonl`. Exact repeat decisions return `duplicate` with `changed=false`. `reviewer_id` is an explicit caller string in V1; it is not authentication.

### Status

`context-cleaner handoff review status --workspace-root <ROOT> --handoff-id <HANDOFF_ID> --json`

This command is read-only. It reports the ledger hash, record count, latest decision per item, approval counts, and whether `approved_memory_write_batch.jsonl` exists. It does not write or refresh artifacts.

### Export approved

`context-cleaner handoff review export-approved --workspace-root <ROOT> --handoff-id <HANDOFF_ID> --json`

This generates or refreshes only:

- `approved_memory_write_batch.jsonl`
- `approval_manifest.json`
- `APPROVAL_REVIEW.md`

Only current latest `approve` items with matching hashes enter the batch. Reject, defer, revoke, pending, invalid, or cross-handoff records cannot enter. Repeated export with unchanged inputs returns `already_generated` and `changed=false`.

All review operations are isolated to the selected handoff directory. They do not write canonical memory, Milvus, aliases, or production databases and do not call an LLM or network service.
