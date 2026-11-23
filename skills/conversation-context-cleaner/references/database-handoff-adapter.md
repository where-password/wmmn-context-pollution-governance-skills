# Database Handoff Adapter

Implementation: `scripts/conversation_cleaner/database_handoff_adapter.py`

This module is a narrow boundary translator between the conversation cleaner's `MemoryWriteHandoffItem` and the database-side `MemoryWriteInput` JSON shape.

It is only a data adapter. It does not authorize writes, approve review decisions, open database connections, resolve real versions, or persist memory.

## Runtime boundary

The runtime adapter uses only the Python standard library and local handoff contracts. It does not import `memory_core`, `pydantic`, SQLite, Milvus, network clients, or an LLM SDK.

The real database model and writer are imported only by the isolated integration test.

## Mapping

`to_memory_write_input(item, *, supersedes_ref_to_id=None, expected_version=None)` returns a JSON-compatible dictionary with exactly the database `MemoryWriteInput` top-level fields.

The following handoff-only fields are removed from the top level and placed under `domain_payload["conversation_handoff"]`:

- `claim_key`
- `scope_key`
- `observed_at`
- `transition_kind`
- `supersedes_ref`

The nested object also records `contract_version=conversation-memory-handoff-v1`.

Existing `domain_payload` entries are preserved. A pre-existing `conversation_handoff` key is rejected to avoid silent overwrite.

## Supersedes resolution

A logical `supersedes_ref` is never converted to a database ID by guessing.

`resolve_supersedes_id()` requires an explicit `supersedes_ref_to_id` mapping. A missing mapping or missing reference blocks conversion. If the handoff item already contains a `supersedes_id`, it must agree with the explicit mapping.

The resulting `MemoryWriteInput.supersedes_id` is still subject to the database writer's authorization, target lookup, active-state, cycle, and ownership checks.

## Versions

The adapter never derives or increments a database version.

`expected_version` is copied only when the handoff item already carries an explicit value or when the caller supplies one. Otherwise it remains `null`. A conflicting explicit value is rejected.

The real database writer remains responsible for active-target checks, optimistic concurrency, version increment, and supersede transaction execution.

## Evidence mapping

Database evidence rows require globally unique `evidence_id` values, while a supersede handoff may retain both old and new observation evidence.

The adapter therefore:

- preserves `source_unit_id`, exact `quote`, `status`, message and character locators;
- preserves the original handoff evidence ID in `locator_json["handoff_evidence_id"]`;
- derives a stable database evidence ID from the item idempotency key plus the original handoff evidence ID.

This prevents cross-memory evidence ID collisions while keeping deterministic repeat output and full source traceability.

## Security boundary

The adapter emits only declared write-input fields. It does not add or trust fields such as:

- `owner_principal_id`
- canonical `sensitivity`
- canonical `visibility`
- `status`
- `memory_id`
- database `version`
- content/source hashes
- security policy results

The database side must authenticate and authorize the caller, validate source ownership and sensitivity, enforce evidence policy, choose the canonical status, approve review, assign IDs/versions, and run the actual transaction.

## Isolated integration validation

The integration test uses the real `memory_core_v1` `MemoryWriteInput`, `MemoryWriter`, SQLiteStore, profile registry, source records, grant, and signed token inside `tempfile.TemporaryDirectory()`.

Execution command:

`$env:PYTHONPATH='memory_core_v1/src'; python -m unittest -v skills.conversation_context_cleaner.tests.test_memory_core_handoff_integration`

Validated behavior:

- adapter output validates as real `MemoryWriteInput`;
- old verified item becomes active with version 1;
- adapted supersedes item resolves `supersedes_ref` to the real old `memory_id`;
- explicit `expected_version=1` is enforced;
- old record becomes `superseded`;
- new record has `version=2`, `supersedes_id` pointing to the old memory, and `pending_review` status because its evidence is unresolved;
- repeated write with the same `MemoryWriteInput` is idempotent and creates no duplicate memory row;
- temporary SQLite, source, grant, token, and evidence data are removed with the temporary directory.

No production canonical database, Milvus collection, alias, or database Skill file is touched.
