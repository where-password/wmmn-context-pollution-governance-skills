# Deterministic Conversation State Resolution

This stage converts reviewable conversation candidates into state observations and resolves conversation-local state transitions. It is a pure data transformation: it does not read or write files, call a clock, inspect environment variables, access a network or database, invoke an LLM, or write canonical/Milvus/alias state.

Implementation: `scripts/conversation_cleaner/state_resolution.py`

The strict records come from `handoff_contracts.py` (`MemoryObservation`, `StateTransition`, `PendingReviewItem`, and `HandoffEvidence`). This module does not define a second record schema.

## Candidate conversion

`candidate_to_observation()` converts a candidate-like mapping or object into a strict `MemoryObservation`.

The caller supplies or embeds:

- `observed_at`: required RFC3339 timestamp with an offset;
- `source_sequence`: required non-negative source order;
- `scope_key`, or enough workspace/project/subject/entity data to derive one;
- `claim_key`, or a candidate/group ID fallback that deliberately avoids merging unrelated claims.

The conversion is not registered in any pipeline. It performs no time inference and no state resolution by itself.

When no explicit `state_kind` is supplied:

- a question-like statement becomes `discussion`;
- an uncertain statement becomes `hypothesis`;
- candidate types are mapped conservatively to `decision`, `fact`, `result`, `task`, `preference`, `constraint`, `conflict`, or `discussion`.

`observation_id` is a deterministic SHA-256-derived ID over source identity, claim, scope, observed time, content, and source unit IDs. Repeating the conversion with the same inputs yields the same observation ID.

## Grouping and ordering

`resolve_state_transitions()` groups observations by:

`scope_key + claim_key + conversation_id`

The conversation component is part of the internal chain identity because observations from different conversations must never automatically supersede or merge. Scope is also part of the group identity, so different scopes cannot overwrite one another.

Within a chain, observations are sorted by:

1. observable time, normalized to UTC;
2. `source_sequence`;
3. `observation_id`.

Sorting is deterministic and independent of input list or dictionary insertion order.

## Transition rules

A first authoritative observation produces `new`.

- Later explicit `decision`, `fact`, or `result` observations may produce `supersedes`.
- `discussion`, `hypothesis`, and `task` observations never overwrite current state; they are retained as `historical_only`.
- A later `constraint` or `preference` is not automatically allowed to replace a conflicting current state; it produces `needs_review`.
- An explicit `conflict` observation always produces `conflict` plus a pending-review item.
- A later `decision`/`fact`/`result` containing clear uncertainty or question language is routed to `needs_review`, not treated as a superseding state.
- Identical same-kind content produces `unchanged`; the existing current state remains current.

Recency is a strong default signal but not an absolute rule. The resolver models conversation state, not permanent truth. A later question, hypothesis, alternative, quotation, or non-final discussion never silently overwrites an earlier explicit state.

## Same-time conflicts

Observations sharing an observable time are evaluated as a bucket. State priorities only break ties when priorities differ:

- `result`: 5
- `fact`: 4
- `decision`: 3
- `constraint` / `preference`: 2
- `task`: 1
- `discussion` / `hypothesis` / `conflict`: 0

If multiple top-priority observations at the same time contain different content, every conflicting observation receives `conflict` and a pending-review item. The previous current state is retained. Evaluation never selects one merely because it appeared later in the input list.

Identical same-time observations are collapsed deterministically by source sequence and observation ID; duplicates receive `unchanged`.

## Evidence and history

Every `StateTransition` retains evidence from the observations involved in the decision. A `supersedes` transition includes evidence from both the previous state and the replacing state. `PendingReviewItem` also carries merged evidence, the primary observation, and related observation IDs.

Original observations are never rewritten. The result exposes:

- all sorted observations;
- one transition per observation;
- pending-review items;
- current observation IDs;
- historical-only IDs;
- superseded IDs;
- conflict IDs;
- needs-review IDs.

## Review separation

Only authoritative explicit states enter `current_observation_ids`. Conflicted or ambiguous observations produce `PendingReviewItem` records and never become automatically writable handoff items.

A current observation is still a review-stage suggestion in the overall pipeline. This resolver does not write memory records or grant promotion.

## Determinism and idempotency

Transition IDs and pending-review IDs are deterministic SHA-256-derived identifiers over the claim, scope, conversation, observation endpoints, transition kind, reason, and merged evidence IDs.

Running the resolver repeatedly over the same observations produces the same result object and the same serialized output. Input ordering does not affect the result. No global mutable state is used.

## Boundaries

- No file I/O or artifact writing.
- No network, database, Milvus, alias, embedding, or LLM calls.
- No environment-variable inspection.
- No wall-clock reads.
- No canonical promotion.
- No CLI or clean plan/apply integration in this task.

A later explicit handoff-export task may convert the resolved current states into `MemoryWriteHandoffItem` records and write isolated handoff artifacts.
