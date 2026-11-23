# Observation Generation

Implementation: `scripts/conversation_cleaner/observation_generation.py`

This module is a pure conversion layer from controlled candidates plus their original messages into strict `MemoryObservation` records. It does not write files, run the cleaner, access a clock or environment, call a database/network/LLM, or modify review state.

It reuses:

- `Candidate`, `Message`, and `EvidenceSpan` from the existing contracts;
- `MemoryObservation` from `handoff_contracts.py`;
- `candidate_to_observation()` and `build_scope_key()` from `state_resolution.py`.

## Entry point

`generate_observations()` receives:

- an explicit candidate collection;
- the corresponding message collection;
- explicit `workspace_id`, `project_id`, `source_id`, `domain_profile`, and `profile_version`;
- optional subject/entity values used only for deterministic scope fallback.

It returns `ObservationGenerationResult` with:

- deterministically sorted strict observations;
- deterministic generation issues;
- observation/issue counts.

The result is ready to pass into `resolve_state_transitions()`.

## Strict message metadata

A message may provide `metadata["observation"]` as an object with only these fields:

| Field | Requirement |
|---|---|
| `claim_key` | Required, non-empty string |
| `scope_key` | Optional non-empty string |
| `state_kind` | Optional valid state kind |
| `observed_at` | Optional RFC3339 timestamp with offset |
| `effective_from` | Optional RFC3339 timestamp with offset |
| `effective_to` | Optional RFC3339 timestamp with offset |

Unknown fields, a missing/empty claim key, non-object metadata, invalid state kinds, malformed timestamps, timezone-less timestamps, or reversed effective intervals raise `ObservationGenerationError` immediately.

Explicit metadata is valid only when the message maps to exactly one observation candidate.

If a message produces multiple candidates but has one observation metadata object, the module emits an `ambiguous_observation_metadata` issue and creates no observations for that message. It never copies one claim key onto several candidates.

## Metadata fallback

When no observation metadata exists:

- `claim_key` becomes `candidate:<candidate_id>`;
- `scope_key` comes from `build_scope_key()` using explicit workspace/project and optional subject/entity;
- `observed_at` comes from the message timestamp;
- `state_kind` uses the existing deterministic candidate type/content inference;
- effective times remain unset.

The fallback claim identity is deliberately candidate-local. Ordinary prose without structured claim identity therefore produces independent observations and cannot accidentally supersede another candidate.

To model a cross-message state transition, the caller must supply a stable, shared `claim_key` through `metadata["observation"]`.

## Candidate/message validation

Each candidate must map to exactly one message:

- candidate conversation/workspace/project/profile must match explicit scope;
- every evidence span must reference an existing message;
- `message_id` and `source_unit_id` must agree;
- evidence role, source-unit hash, character bounds, and quoted slice must match the message;
- evidence status must be a valid contract status;
- one candidate may not span multiple messages.

Invalid or conflicting correspondence blocks generation instead of guessing.

Candidate evidence is passed through unchanged by `candidate_to_observation()`: evidence ID, source unit, exact quote, character span, locator metadata, and verified status remain traceable.

## Determinism

Observation IDs bind:

- explicit source/profile identity;
- conversation/message/source unit;
- candidate ID;
- claim and scope identity;
- observed time and content.

Observations are sorted by `conversation_id`, `source_sequence`, and `observation_id`. Issues are sorted by conversation, sequence, message ID, and issue ID. Reordering candidate or message inputs does not change the result.

Repeated generation over the same immutable inputs is fully idempotent.

## State transition handoff

Example controlled pattern:

1. Message A metadata: `claim_key=claim:memory-upgrade`, `state_kind=constraint`.
2. Message B metadata: same `claim_key`, `state_kind=fact`.
3. `generate_observations()` creates two observations.
4. `resolve_state_transitions()` emits `supersedes` for B against A.

Without steps 1-2, the fallback candidate-local claims intentionally prevent this supersede relationship.

## Boundary

This layer only generates review-stage observations. It does not decide canonical truth, approve review, write memory, modify Milvus/aliases, or attach the generator to the clean plan/apply pipeline. Those integrations require later explicit API/version work.
