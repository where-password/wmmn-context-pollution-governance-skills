# Handoff Approval Ledger and Approved Batch V1

## Purpose

`handoff_review.py` persists human decisions for one exported handoff directory and derives a deterministic approved batch. It does not write canonical memory, Milvus, aliases, or database rows.

The exported `memory_write_batch.jsonl` remains the immutable proposal set. The approval ledger records decisions about those items; the approved batch is a generated subset that contains only the current effective `approve` items.

## Ledger

`record_handoff_decision()` appends a strict `HandoffApproval` record to:

`approval_ledger.jsonl`

Every record contains:

- `approval_id`
- `handoff_id`
- `item_idempotency_key`
- `item_sha256`
- `decision` (`approve`, `reject`, `defer`, or `revoke`)
- `reviewer_id`
- `reason`
- `reviewed_at`

Rules:

- The handoff ID must match `handoff_manifest.json`.
- The item must exist in the current `memory_write_batch.jsonl` with the same full canonical item hash.
- Every append rewrites the complete validated ledger with an atomic file replacement.
- An exact duplicate decision returns `duplicate` with `changed=false` and does not add a line.
- Two different decisions for the same handoff, item key, item hash, and reviewed instant are a conflict and are blocked, even if their timezone spellings differ.
- Corrupt JSON, empty records, unknown fields, invalid approval IDs, and ledger secret-scan findings block reads without being overwritten.
- Exact duplicate lines are collapsed deterministically when reading; conflicting duplicate approval IDs are blocked.

Reviewer identity is only a caller-provided string in V1. It is not authentication.

## Latest-decision rule

For each current handoff item:

1. Select all ledger records whose handoff, idempotency key, and item hash match the current item.
2. Sort by parsed `reviewed_at` instant, then `approval_id` as the deterministic tie-breaker.
3. The greatest record is the latest decision.

Only the latest decision controls the item. Therefore a later `revoke` overrides an earlier `approve`; a later `approve` can supersede an earlier `revoke`. `reject`, `defer`, `revoke`, and no decision never enter the approved batch.

Cross-handoff records, records for unknown item keys, and hash-mismatched records block generation rather than being silently ignored.

## Generated artifacts

`generate_approved_batch()` reads the current proposal batch and ledger and writes only inside the same handoff directory:

- `approved_memory_write_batch.jsonl`: original handoff items whose latest decision is `approve`
- `approval_manifest.json`: counts, ledger hash, approved-batch hash, and safety flags
- `APPROVAL_REVIEW.md`: latest decision, reviewer, reason, reviewed time, and hash status for every item

Items are sorted deterministically by conversation, observed time, source sequence, and idempotency key. Regenerating unchanged inputs returns `already_generated` and `changed=false`; files are byte-identical.

Manifest counts distinguish:

- `approved_count`
- `rejected_count`
- `deferred_count`
- `revoked_count`
- `pending_count`

`ledger_sha256` hashes the exact ledger bytes. `approved_batch_sha256` hashes the generated batch text. Safety flags are always:

- `production_writes=false`
- `real_llm_called=false`
- `milvus_written=false`
- `alias_changed=false`

## Security and authorization boundary

This layer does not verify reviewer credentials, prevent an authorized caller from fabricating a reviewer string, persist decisions outside the handoff directory, or authorize database writes. The database integration must still validate approval provenance, authorization, source integrity, expected versions, sensitivity policy, replay behavior, and transaction semantics.

`APPROVAL_REVIEW.md` is review evidence, not proof that a database write occurred.
