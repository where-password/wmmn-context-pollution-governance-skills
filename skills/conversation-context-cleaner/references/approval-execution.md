# Approval Execution and Post-Write Revocation V1

## Purpose

`approval_execution.py` builds a deterministic database execution plan from a handoff directory. It does not execute database writes itself. The database integration must call this module or implement equivalent independent revalidation before consuming a handoff or approved batch.

The handoff proposal, approval ledger, generated approved batch, and approval manifest are all treated as untrusted inputs until their hashes and identities are revalidated.

## Independent checks

The planner validates:

- `handoff_manifest.json` exists and its `handoff_id` matches the requested handoff.
- Every artifact hash recorded in the handoff manifest matches the current file.
- `approval_manifest.json` exists, belongs to the same handoff, and all safety flags are false.
- `ledger_sha256` and `approved_batch_sha256` in the approval manifest match the current ledger and batch bytes.
- Every approved-batch item exists in the current handoff proposal and has the same complete item hash.
- Every ledger record belongs to the current handoff, uses a current item key, and binds to the current item SHA-256.
- Each item's latest decision is recomputed from the ledger using `reviewed_at` and `approval_id`; the approved batch is never trusted as the decision source.

Any mismatch blocks the plan. A stale or tampered approved batch cannot authorize an item.

## Written memory mapping

The caller must explicitly provide:

`item idempotency key -> {memory_id, version}`

The mapping is required even when empty. Unknown keys, malformed values, duplicate memory IDs, non-positive versions, and missing explicit values are rejected. The planner never invents a `memory_id` or version.

## Execution-plan semantics

The deterministic plan contains:

- `approved_write_items`: item plus its currently effective approval, only when the latest decision is `approve` and the item is not known to have been written.
- `blocked_items`: reject, defer, pending, or otherwise non-writable items.
- `already_applied_items`: latest `approve` items present in the explicit written-memory map; these are not proposed for another write.
- `post_write_revocations`: latest `revoke` items with an explicit memory mapping; each contains a `MemoryDeleteInput`-compatible dict.
- `pending_post_write_revocations`: latest `revoke` items without a mapping; no target or version is invented.
- `counts`: deterministic totals for the above categories and all items.

A direct `revoke` with no prior approval and no written-memory mapping is blocked as `revoked_before_write`; it never enters an executable write. If an earlier `approve` exists but the memory mapping is absent, the item enters `pending_post_write_revocations` because the database may already contain the record. A later `approve` can legitimately become current again according to the latest-decision rule.

## MemoryDeleteInput compatibility

Post-write revocation emits a dict with:

- `workspace_id` from the handoff item
- `target_type=memory_id`
- `target_id` from the explicit written-memory map
- `expected_version` from the map
- `delete_reason` from the latest revoke reason
- deterministic `idempotency_key` and `operation_id` bound to the approval ID, memory ID, workspace, target, and version

The generated reason is secret-scanned. The planner does not delete any record; a database service must execute the delete request under its own authorization and transaction rules.

## Tombstone boundary

Revoke after write means a durable deletion/tombstone intent, not physical erasure. The database implementation must preserve the historical row and evidence according to its retention policy. Repeating the same delete request must be idempotent through the database service.

`MemoryDeleteService` in the isolated database environment is used by tests as the public execution boundary. No `memory_core` source is modified.

## Side-effect boundary

Building a plan performs no file writes, clock reads, network calls, LLM calls, database writes, Milvus writes, or alias changes. The only permitted side effect in this task is an isolated temporary SQLite test database.
