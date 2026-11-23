# Contract V1

## Public boundary

The stable module is `scripts/conversation_cleaner/api_v1.py`. Its frozen public API version is `1.5.0` and its contract version is `conversation-context-cleaner-v1.5.0`; the deterministic extractor version remains `deterministic-rules-v13`.

Public calls:

- `doctor(workspace_root, input_root=None, input_path=None)`
- `clean_plan(...)`
- `clean_apply(plan, confirmation_token=...)`
- `status(workspace_root=...)`
- `review_export(workspace_root=..., run_id=..., output_dir=None)`

`conversation_cleaner.ConversationSkillApi` exposes the same five callables. Any incompatible shape or behavior change requires a new API version.

## Input contract

The input contract is `controlled_conversation_jsonl_v1_snapshot`.

Each non-empty line is one JSON object with:

- `conversation_id`: non-empty string and member of the explicit conversation allowlist.
- `message_id`: non-empty string, unique within the conversation.
- `role`: `user` or `assistant`.
- `timestamp`: RFC3339 timestamp with an explicit offset.
- `sequence`: non-negative integer, unique and contiguous within the conversation.
- `text`: non-empty string.
- `attachments`: array. Empty arrays are preferred in V1. Non-empty entries must contain attachment ID, name, media type, size, SHA-256, locator, and extraction status.
- Optional: `event_type`, `source_revision`, `metadata`.

V1 is a snapshot contract and accepts `event_type=message` only. Revision replay, edit chains, and delete tombstones require a future API version.

Topic metadata supported by V1:

- `metadata.topic_key`: explicit topic identity.
- `metadata.topic_boundary=true`: force a segment boundary.
- A first-line marker `[[topic: key]]` or `#topic: key`.

## Stable identifiers

For each parsed message:

- `timestamp_utc` is derived from the explicit offset.
- `source_unit_hash = sha256(canonical_json(message payload))`.
- `revision_id = source_unit_hash[:16]`.
- `source_unit_id = "su_" + sha256(canonical_json([source_id, conversation_id, message_id, revision_id]))[:32]`.

The plan identity binds API version, extractor version, scope, request, input SHA-256, and the extraction digest. Changing any source byte after planning makes the old plan stale.

## Output contract

`clean apply` writes only under:

`<workspace-root>/conversation-cleaner/v1/runs/<run_id>/`

Artifacts:

- `messages.jsonl`: normalized, hash-addressed source messages.
- `segments.jsonl`: deterministic topic segments, boundary reasons, and trivial-segment merge audit.
- `raw_candidates.jsonl`: every original extracted candidate and its original evidence spans.
- `candidates.jsonl`: consolidated long-term candidate groups. A group keeps member candidate IDs, all member evidence, `child_claims`, deterministic synthesis content, `decision_origin`, `parent_group_ids`, `overlap_with`, merge reasons, `claim_key`, and a stable `group_id`.
- `parent_groups.jsonl`: higher-level groups linking multiple consolidated groups or child parent groups through shared structure context or rare identifiers; no pilot-specific business terms are production triggers. Parent groups can be nested.
- `task_history.jsonl`: process tasks and stage promises routed out of long-term candidates. Tasks are retained and may be `open`, `completed`/archived, `needs_user_confirmation`, or `resolved_to_constraint`.
- `noise_items.jsonl`: short/context-dependent heuristic fragments with summary, reason, and source message.
- `open_questions.jsonl`: unresolved questions, always `status=open` and `resolved=false`; open-question candidates are not grouped as long-term candidates.
- `suppressed_candidates.jsonl`: per-item audit for extraction caps, duplicate/question suppression, task routing, and noise filtering. It stores a short summary, reason, source message, and `full_text_included=false`.
- `observations.jsonl`: strict `MemoryObservation` records generated from controlled candidates/messages. Claim/scope identity and state kind are validated before apply.
- `state_transitions.jsonl`: deterministic `StateTransition` records from the conversation-state resolver, including supersedes/conflict/needs-review transitions and evidence.
- `state_review.jsonl`: pending `PendingReviewItem` records from state resolution. These are never writable handoff items automatically.
- `review_bundle.jsonl`: heterogeneous suggestion-only items for top-level `parent_group`, unparented `grouped_candidate`, `task_item`, `noise_item`, and `open_question`. Nested child groups and child parent groups are referenced inside parent_group items and are never duplicated at top level.
- `review_stats.json`: before/after counts, top-level and nested group counts, merge/task/noise/suppression statistics, observation/state counts, and suggestion metadata. Observation/state counts are `observations`, `state_transitions`, `pending_state_reviews`, and `observation_generation_issues`; legacy candidate fields remain. `candidate_count` is retained as a compatibility alias for the top-level grouped-candidate count.
- `REVIEW_BUNDLE.md`: human-readable review summary.
- `audit_report.json`: source hashes, counts, checks, and safety receipts.
- `run_manifest.json`: artifact hashes and run identity.

All candidates carry explicit `workspace_id` and `project_id`. No record has a canonical or Milvus visibility path.

`clean plan` performs observation generation and state resolution in memory and blocks on generation errors or `ambiguous_observation_metadata`; no workspace/run is created while blocked. `clean apply` writes the three state artifacts above, includes them in `run_manifest.json`, and `review export` copies them with export-manifest hashes. `clean apply` never invokes `handoff export` automatically. The frozen API version is `1.5.0`; this Skill is an independent database preprocessing module and does not write canonical memory, Milvus, or aliases.

Raw/heuristic candidates carry `extraction_method` (`explicit_marker` or `heuristic_fallback`), `heuristic_rule`, `structure_kind`, and `promotion_eligible=false`. The plan exposes configurable heuristic caps plus `extractor_summary` and `consolidation_summary`; the run audit records raw candidates, grouped candidates, parent groups, grouped members, deterministic synthesis merge reasons, task items, noise items, open questions, suppressed reasons, and topic-merge audit.

## V1.5.0 freeze boundary

`conversation-context-cleaner` is an independent Skill and the preprocessing module for the database Skill. Its outputs are review-only artifacts. `handoff export`, approval ledger decisions, approved-batch generation, and approval execution plans are review or handoff boundaries, not canonical writes.

The database Skill must independently validate approval provenance, item hashes, latest decisions, authorization, versions, and transactions before any write. Revoke before execution removes pending authorization; revoke after a recorded write produces only a deletion/tombstone intent and must not physically erase history.

The release keeps the deterministic extractor version `deterministic-rules-v13`; no new runtime capability is introduced by this version freeze.
