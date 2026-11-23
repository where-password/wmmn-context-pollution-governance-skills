# Handoff Approval Gate V1

## Purpose

The database handoff adapter accepts a handoff item only when an explicit human approval is bound to the exact handoff and item content. Handoff export remains review-only; approval authorizes a conversion to `MemoryWriteInput`, not a canonical write by itself.

The database side must still enforce authorization, source/source-unit integrity, expected versions, security policy, and the transaction. Approval records are review evidence, not canonical memory.

The earlier temporary evidence-downgrade workaround described in `references/handoff-pilot.md` is historical. This approval contract supersedes that workaround: verified evidence stays verified, and an explicitly approved write may become active.

## Approval contract

`HandoffApproval` is a strict standard-library-only record with exactly these fields:

| field | meaning |
|---|---|
| `approval_contract_version` | must equal `conversation-memory-handoff-approval-v1` |
| `approval_id` | deterministic ID derived from every other approval field and the approval contract version |
| `handoff_id` | handoff directory identity |
| `item_idempotency_key` | approved `MemoryWriteHandoffItem` identity |
| `item_sha256` | canonical hash of the complete handoff item |
| `decision` | `approve`, `reject`, `defer`, or `revoke` |
| `reviewer_id` | explicit reviewer identity |
| `reason` | non-empty human-readable reason |
| `reviewed_at` | RFC3339 timestamp with an explicit timezone offset |

`create_handoff_approval()` never reads a clock. The caller supplies `reviewed_at`. Every serialized approval record contains `approval_contract_version="conversation-memory-handoff-approval-v1"`; strict `from_dict()` requires that exact value. Unknown or missing fields are rejected, and a tampered `approval_id` is rejected by recomputation.

## Item binding

`handoff_item_sha256()` serializes the complete `MemoryWriteHandoffItem.to_dict()` result with canonical JSON:

- sorted object keys
- compact separators
- UTF-8
- no NaN or infinity

The hash therefore covers content, evidence, source scope, transition kind, expected version, supersedes references, domain payload, and all other item fields. Changing any field changes the hash.

`bind_approval_to_item()` / `require_handoff_approval()` rejects:

- a non-`approve` decision
- a different `handoff_id`
- a different idempotency key
- a different item SHA-256

## Adapter boundary

`database_handoff_adapter.to_memory_write_input()` now requires:

- `handoff_id`
- an approval record that is `approve` and bound to the exact item

The adapter rejects missing, `reject`, `defer`, `revoke`, cross-handoff, cross-key, and hash-mismatched approvals. After approval, the existing mapping rules remain:

- database `MemoryWriteInput` top-level fields stay strict
- evidence IDs are deterministically stabilized
- evidence records and locators are preserved
- handoff-only fields are placed under `domain_payload.conversation_handoff`
- `approval_id` is added to that metadata
- `expected_version` is not inferred
- `supersedes_id` requires an explicit caller-provided map

A successfully approved and verified handoff can be written as active by `MemoryWriter`. That is valid because the human approval happened before conversion; unresolved evidence is not used to simulate pending review.

## Isolation and side effects

Approval creation and binding perform no file, network, database, Milvus, alias, environment, or clock operations. Durable decisions are managed separately by `handoff_review.py`; see `references/handoff-review.md`. Any SQLite validation must use an isolated temporary database unless a separate authorized production integration task says otherwise.

## Remaining boundary

The record contract does not authenticate reviewers or provide a database-side approval lookup. Reviewer identity is only an explicit caller-provided string in V1. Production integration must add an authentication layer, define replay protection across systems, and audit approval provenance before use.
