# Conversation Memory Handoff Contract V1

Contract identifier: `conversation-memory-handoff-v1`

This document defines a pure, machine-readable data contract between `conversation-context-cleaner` and the database-side memory writer. It does not define or execute a CLI, state resolver, canonical write, Milvus write, alias change, embedding call, or promotion path.

The implementation is standard-library-only in `scripts/conversation_cleaner/handoff_contracts.py`. The matching JSON Schema is `references/memory-handoff-v1.schema.json`.

## Boundary

- A handoff item is a proposed database write input. It is not proof that a write occurred.
- `HandoffManifest` must declare `production_writes=false`, `canonical_writes=false`, `milvus_writes=false`, and `alias_changes=false`.
- Pending or conflicted material belongs in `PendingReviewItem` and is never automatically writable.
- The cleaner does not decide review outcomes or promotion eligibility in this contract.
- The contract contains no authentication, authorization, ownership, canonical sensitivity/visibility enforcement, database IDs, or storage status fields.
- `declared_sensitivity` and `requested_visibility` are database input declarations, not grants or security decisions.
- `contains_security_fields` in the manifest must always be `false`.

## Records

All records are frozen data classes with deterministic `to_dict()` output and strict `from_dict()` input. Unknown fields are rejected at every level; extra data must not be silently discarded.

### `MemoryObservation`

Captures one reviewable claim observation without resolving conflicts or time ordering.

| Field | Type | Notes |
|---|---|---|
| `observation_id` | string | Stable observation identity |
| `conversation_id` | string | Source conversation |
| `source_sequence` | integer | Non-negative source order |
| `observed_at` | RFC3339 date-time | Must include an offset |
| `claim_key` | string | Deterministic logical claim identity |
| `scope_key` | string | Workspace/project/subject/entity scope |
| `memory_type` | enum | Database memory type vocabulary |
| `content` | string | Claim content |
| `assertion_source` | enum | Database assertion-source vocabulary |
| `verification_state` | enum | Database verification-state vocabulary |
| `epistemic_status` | string | Caller-defined epistemic status |
| `state_kind` | enum | `discussion`, `hypothesis`, `decision`, `fact`, `task`, `result`, `preference`, `constraint`, or `conflict` |
| `evidences` | array | At least one evidence record |
| `confidence` | number | `0.0..1.0` |
| `effective_from` / `effective_to` | nullable date-time | Effective interval metadata |
| `review_status` | enum | Defaults to `pending_review` |

Evidence records contain `evidence_id`, `source_unit_id`, exact `quote`, `locator_json`, and `status` (`verified`, `rejected`, or `unresolved`).

### `StateTransition`

Records a logical transition. It does not write history or mutate prior observations.

Required fields are `transition_id`, `claim_key`, `scope_key`, `to_observation_id`, `transition_kind`, `reason`, and `evidences`. Optional fields are `from_observation_id`, `confidence`, and `review_status`.

Allowed transition kinds are exactly:

`new`, `unchanged`, `supersedes`, `conflict`, `needs_review`, `historical_only`.

A `new` transition must have `from_observation_id=null`. Transition evidence is preserved with the transition.

### `MemoryWriteHandoffItem`

Maps the database-side `MemoryWriteInput` concepts plus handoff-only semantic fields. The canonical contract fields are:

`idempotency_key`, `operation_id`, `workspace_id`, `project_id`, `task_id`, `conversation_id`, `domain_profile`, `profile_version`, `memory_type`, `subtype`, `subject`, `entity_type`, `entity_id`, `content`, `importance`, `declared_sensitivity`, `requested_visibility`, `assertion_source`, `verification_state`, `effective_from`, `effective_to`, `source_id`, `source_unit_id`, `source_scope`, `confidence`, `supersedes_ref`, `supersedes_id`, `expected_version`, `domain_payload`, and `evidences`.

Handoff-only fields are:

- `claim_key`: logical claim identity;
- `scope_key`: logical scope identity;
- `observed_at`: observation time used by downstream state resolution;
- `transition_kind`: the proposed transition semantic;
- `supersedes_ref`: logical reference before a database ID exists;
- `supersedes_id`: concrete database ID only when supplied by an authorized caller;
- `expected_version`: optional optimistic concurrency expectation;
- `evidences`: exact evidence records carried through the handoff.

`source_scope` uses the database input shape: `scope_type`, `ref`, optional `start`/`end`, and `locator_json`.

A `supersedes` item must provide `supersedes_ref` or `supersedes_id`. No item invents canonical IDs or security fields.

### `PendingReviewItem`

A non-writable review record for `conflict` or `needs_review` transitions. It references at least one observation and preserves evidence and a manual-review reason. It cannot represent an accepted decision and has no automatic promotion flag.

### `HandoffManifest`

Binds the contract version, source plan/run, source hash, workspace/project, conversation allowlist, domain/profile, artifact hashes, and counts. It asserts the no-production-write boundary and must set `contains_security_fields=false`.

Required manifest fields include:

- `handoff_id`
- `run_id` and `plan_id`
- `source_sha256` and `observations_sha256`
- `workspace_id` and `project_id`
- `conversation_ids`: a non-empty string array derived from the exported observations and sorted with duplicates removed
- `domain_profile` and `profile_version`
- `artifact_hashes`
- `counts`
- `contract_version`
- `production_writes=false`, `canonical_writes=false`, `milvus_writes=false`, `alias_changes=false`, and `contains_security_fields=false`

The exported artifact `handoff_manifest.json` must include the same `conversation_ids` value; it is not optional.

## `source_units.jsonl`

The handoff directory also contains `source_units.jsonl`. It contains exactly one sorted, de-duplicated record for each source unit referenced by the handoff evidence. Each record must include:

- `source_id`, `workspace_id`, `project_id`
- `source_unit_id`, `parent_unit_id`, `scope_type`, `scope_ref`
- `source_unit_hash`, `text_content`
- `conversation_id`, `message_id`, `role`, `sequence`, `timestamp`
- `source_type="conversation_message"`
- `source_path="conversation:<conversation_id>"`
- `source_sha256` from the source run
- `visibility`, `sensitivity` derived from the corresponding handoff item
- `metadata` containing the same message identity fields

The exporter derives `text_content`, hashes, timestamps, roles, and sequences from the run's `messages.jsonl`, not from evidence quotes. Evidence quotes must be exact substrings of the source text. A source unit with conflicting visibility/sensitivity across handoff items blocks export.

The database Skill must validate and register these source/source-unit records, including the source-unit hash and text, before accepting a corresponding `MemoryWriteInput`. `source_units.jsonl` does not authorize a write by itself.

## Idempotency

`generate_idempotency_key()` accepts a `MemoryWriteHandoffItem` or a mapping suitable for strict validation.

The generated value is:

`mwh_` + SHA-256 of canonical JSON containing the contract version and the complete item payload except `idempotency_key` itself.

The key is stable across:

- repeated calls for the same logical item;
- evidence list reordering;
- equivalent timestamp offsets because timestamps are canonicalized to UTC.

It changes when any semantic field changes, including source unit, source profile, claim key, scope key, conversation, content, observed time, transition kind, supersedes state, expected version, or evidence content.

`materialize_idempotency_key()` validates a mapping and returns the item with the generated key.

## Review separation

- Stable/explicit current-state material may become a `MemoryWriteHandoffItem` only after a future explicit handoff selection step.
- Conflicted, uncertain, or unresolved material must become a `PendingReviewItem`.
- A pending review item must never be copied into a writable batch.
- This contract alone does not produce any artifact files. Artifact writing belongs to a later explicit handoff-export task.
