# Handoff State Transition Pilot V1

## Purpose

This pilot validates the deterministic path from a controlled conversation through:

`clean plan` -> `clean apply` -> `handoff export` -> duplicate `handoff export`

It verifies that an earlier “do not upgrade memory” state is retained as history while a later explicit “already upgraded to 32GB” statement becomes the current proposed write with a `supersedes` transition.

This is a review-only database handoff pilot. It does not write canonical memory, Milvus, aliases, or production data and does not call an LLM or network service.

## Input fixture

`assets/synthetic/handoff_state_transition_conversation.jsonl`

One synthetic conversation, `conv_synthetic_handoff_upgrade_001`, with three messages:

| sequence | role | content |
|---:|---|---|
| 0 | user | 暂时不升级内存，因为没有预算。 |
| 1 | assistant | 收到，当前保持现有内存配置。 |
| 2 | user | 已经升级到 32GB 内存。 |

The fixture is registered under `additional_fixtures` in `assets/synthetic/fixture_manifest.json`.

## Dynamic observations

The pilot does not infer `claim_key` from cleaner candidates. It reads the applied run's `messages.jsonl` and constructs two strict `MemoryObservation` records from the exact source messages.

Both observations use:

- `claim_key`: `claim:device-memory-upgrade`
- `scope_key`: deterministic workspace/project/device-memory scope
- exact `source_unit_id`
- exact quote and character span
- exact message timestamp and source sequence
- `quote_verified` evidence status

State sequence:

| observation | source sequence | state kind | role in resolution |
|---|---:|---|---|
| `obs_memory_not_upgraded` | 0 | `constraint` | historical state |
| `obs_memory_upgraded_32gb` | 2 | `fact` | current state and superseding write |

## Expected result

- two state transitions: one `new`, one `supersedes`
- exactly one write item: `已经升级到 32GB 内存。`
- no write item for the first state
- write item `transition_kind=supersedes`
- write item `supersedes_ref=obs_memory_not_upgraded`
- `supersedes_id=null`
- both observation evidences retained on the write item
- empty `review_pending.jsonl`
- historical first observation visible in skipped/transition/review audit
- duplicate export returns `already_exported` and `changed=false`

## Persistent pilot

Pilot root:

`work/pilot/handoff_upgrade_pilot_v1`

Final workspace root:

`work/pilot/handoff_upgrade_pilot_v1/workspace_current`

Applied run:

`ccr_48595073c605073f55798ce4`

Handoff:

`handoff_8af479c62abc438751f1cb3a`

Artifact directory:

`work/pilot/handoff_upgrade_pilot_v1/workspace_current/conversation-cleaner/v1/handoffs/handoff_8af479c62abc438751f1cb3a`

Stored final pilot inputs/responses:

- `clean_plan_current.json`
- `clean_apply_current.json`
- `observations_current/memory_upgrade_observations.jsonl`
- `handoff_export_first_current.json`
- `handoff_export_second_current.json`

Earlier pilot artifacts remain under the same ignored pilot root and were not deleted.

## Actual result

- state transitions: `2`
- write items: `1`
- `superseding_writes`: `1`
- `new_writes`: `0`
- pending review: `0`
- skipped observations: `1`
- first export: `exported`, `changed=true`
- duplicate export: `already_exported`, `changed=false`
- artifact hashes remained unchanged across repeated export
- all manifest safety flags are false
- manifest artifact SHA-256 values match the files on disk

## Manual `HANDOFF_REVIEW.md` inspection

Conclusion: passed.

- The `## Current writes` section contains no non-superseding new write for this pilot.
- The `## Supersedes` section contains the current state `已经升级到 32GB 内存。` and explicitly references `obs_memory_not_upgraded`.
- `## Skipped` records `obs_memory_not_upgraded` with the reason that it was superseded before the final batch.
- `state_transitions.jsonl` retains the old evidence quote, the `new` transition for the historical state, and the `supersedes` transition linking the old observation to the 32GB observation.
- `暂时不升级内存，因为没有预算。` does not appear as a current write and is not presented as the current long-term state.
- `review_pending.jsonl` is empty.
- The document states that no canonical database, Milvus, or alias write has occurred and that it is not proof of persistence.

Therefore the earlier non-upgrade statement is not presented as the current long-term state. The current proposed state is 32GB with an explicit supersedes relationship.

## Test coverage

`tests/test_handoff_pilot.py` runs the full workflow in a temporary isolated workspace and locks:

- fixture shape and single conversation
- dynamic observation evidence construction
- supersedes generation
- old state exclusion from the write batch
- current 32GB write
- empty pending review
- review document historical/current separation
- manifest safety fields
- artifact hash stability
- duplicate export no-op behavior

The test uses only temporary local files and does not access network, LLMs, production databases, Milvus, aliases, or `.env` files.

## End-to-end isolated SQLite validation

`tests/test_end_to_end_handoff.py` validates the complete controlled path:

`conversation JSONL + observation metadata` -> `clean plan` -> `clean apply` -> run-local `observations.jsonl` / `state_transitions.jsonl` -> `handoff export` -> database adapter -> `MemoryWriteInput` -> `MemoryWriter` -> temporary SQLite.

### Inputs

The test uses one logical conversation and two incremental snapshots:

| run | controlled messages | expected cleaner result |
|---|---|---|
| Run 1 | `DECISION: 暂时不升级内存，因为没有预算。` | 1 observation, 1 `new` transition, 1 handoff write |
| Run 2 | the same first message plus `FACT: 已经升级到 32GB 内存。` | 2 observations, 2 transitions including `supersedes`, 1 superseding handoff write |

Both runs use the same explicit `claim_key` and deterministic workspace/project scope. The handoff export reads each run directory's real `observations.jsonl`; the test does not handcraft `memory_write_batch.jsonl`, `state_transitions.jsonl`, or `HANDOFF_REVIEW.md`.

### Database boundary

The test creates all of the following under one `tempfile.TemporaryDirectory()`:

- both Skill input directories and the shared workspace/run/handoff tree
- the SQLite database
- workspace/project records
- source and source-unit records derived from the actual run messages
- access grant and signed tokens
- `ProfileRegistry` and `MemoryWriter`

No production path is referenced. Both handoff manifests are checked for `production_writes=false`, `real_llm_called=false`, `milvus_written=false`, `alias_changed=false`, `canonical_writes=false`, and `contains_security_fields=false`.

### Actual result

- Run 1 handoff contains one `new` write. The adapter produces a valid `MemoryWriteInput`; the temporary database stores it as `active`, version `1`.
- Run 2 handoff contains one `supersedes` write. Its `supersedes_ref` matches Run 2's historical observation ID and is explicitly resolved to the Run 1 `memory_id`.
- The adapter preserves the old and new evidence quotes and places `claim_key`, `scope_key`, `observed_at`, `transition_kind`, and `supersedes_ref` under `domain_payload.conversation_handoff`.
- The old database row becomes `superseded`.
- The new database row is version `2`, has `supersedes_id` pointing to the old row, remains `pending_review`, and contains both evidence quotes.
- Repeating the identical Run 2 `MemoryWriteInput` returns the same memory ID and version and leaves the total row count at `2`.

### Manual-review gate

The adapter intentionally preserves the handoff evidence status as `verified`. `MemoryWriter` currently promotes any write containing verified evidence to `active`. Because this pilot is review-only, the isolated test applies a database-side manual-review gate after adapter mapping: proposed superseding evidence is downgraded to `unresolved` before strict `MemoryWriteInput` validation. That policy is local to the test and does not change runtime code.

Consequently the test proves the application-to-adapter-to-SQLite chain and the supersede transaction, but it does not prove that direct production adapter output would be `pending_review`. Moving the review gate into the production database integration boundary remains a separate review decision and implementation task.
