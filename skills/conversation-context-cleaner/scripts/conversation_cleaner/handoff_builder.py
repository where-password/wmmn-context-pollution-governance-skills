from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from .handoff_contracts import (
    HANDOFF_CONTRACT_VERSION,
    HandoffEvidence,
    HandoffSourceScope,
    MemoryObservation,
    MemoryWriteHandoffItem,
    PendingReviewItem,
    StateTransition,
    materialize_idempotency_key,
)


WRITABLE_STATE_KINDS = frozenset({"decision", "fact", "result", "preference", "constraint"})
PENDING_TRANSITION_KINDS = frozenset({"conflict", "needs_review"})


class HandoffBuildError(ValueError):
    """Raised when resolved observations cannot be converted safely to handoff data."""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash_id(prefix: str, value: Any) -> str:
    return prefix + hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()[:24]


def _observation_sort_key(observation: MemoryObservation) -> tuple[str, int, str]:
    return (observation.conversation_id, observation.source_sequence, observation.observation_id)


def _merge_evidences(records: Iterable[Any]) -> tuple[HandoffEvidence, ...]:
    by_id: dict[str, HandoffEvidence] = {}
    for record in records:
        values = record.evidences if hasattr(record, "evidences") else record
        for evidence in values:
            if not isinstance(evidence, HandoffEvidence):
                raise HandoffBuildError("evidence records must be HandoffEvidence instances")
            existing = by_id.get(evidence.evidence_id)
            if existing is not None and existing.to_dict() != evidence.to_dict():
                raise HandoffBuildError(f"evidence_id {evidence.evidence_id} has conflicting payloads")
            by_id[evidence.evidence_id] = evidence
    return tuple(by_id[key] for key in sorted(by_id))


def _primary_evidence(observation: MemoryObservation) -> HandoffEvidence:
    if not observation.evidences:
        raise HandoffBuildError("write observations must retain at least one evidence record")
    return min(
        observation.evidences,
        key=lambda item: (0 if item.status == "verified" else 1, item.source_unit_id, item.evidence_id),
    )


@dataclass(frozen=True)
class HandoffBuildCounts(Mapping[str, int]):
    observations: int
    transitions: int
    write_items: int
    pending_review_items: int
    skipped_observations: int

    def __post_init__(self) -> None:
        for field_name in ("observations", "transitions", "write_items", "pending_review_items", "skipped_observations"):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise HandoffBuildError(f"counts.{field_name} must be a non-negative integer")

    def to_dict(self) -> dict[str, int]:
        return {
            "observations": self.observations,
            "transitions": self.transitions,
            "write_items": self.write_items,
            "pending_review_items": self.pending_review_items,
            "skipped_observations": self.skipped_observations,
        }

    def __getitem__(self, key: str) -> int:
        try:
            return self.to_dict()[key]
        except KeyError as exc:
            raise KeyError(f"unknown handoff count: {key}") from exc

    def __iter__(self):
        return iter(self.to_dict())

    def __len__(self) -> int:
        return len(self.to_dict())


@dataclass(frozen=True)
class SkippedObservation:
    observation_id: str
    conversation_id: str
    source_sequence: int
    transition_id: str
    transition_kind: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "observation_id": self.observation_id,
            "conversation_id": self.conversation_id,
            "source_sequence": self.source_sequence,
            "transition_id": self.transition_id,
            "transition_kind": self.transition_kind,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class HandoffBuildResult:
    write_items: tuple[MemoryWriteHandoffItem, ...]
    pending_review_items: tuple[PendingReviewItem, ...]
    skipped_observations: tuple[SkippedObservation, ...]
    counts: HandoffBuildCounts
    contract_version: str = HANDOFF_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.contract_version != HANDOFF_CONTRACT_VERSION:
            raise HandoffBuildError(f"contract_version must be {HANDOFF_CONTRACT_VERSION}")
        if not all(isinstance(item, MemoryWriteHandoffItem) for item in self.write_items):
            raise HandoffBuildError("write_items must contain MemoryWriteHandoffItem instances")
        if not all(isinstance(item, PendingReviewItem) for item in self.pending_review_items):
            raise HandoffBuildError("pending_review_items must contain PendingReviewItem instances")
        if not all(isinstance(item, SkippedObservation) for item in self.skipped_observations):
            raise HandoffBuildError("skipped_observations must contain SkippedObservation instances")
        if self.counts.write_items != len(self.write_items):
            raise HandoffBuildError("write item count mismatch")
        if self.counts.pending_review_items != len(self.pending_review_items):
            raise HandoffBuildError("pending review count mismatch")
        if self.counts.skipped_observations != len(self.skipped_observations):
            raise HandoffBuildError("skipped observation count mismatch")

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "write_items": [item.to_dict() for item in self.write_items],
            "pending_review_items": [item.to_dict() for item in self.pending_review_items],
            "skipped_observations": [item.to_dict() for item in self.skipped_observations],
            "counts": self.counts.to_dict(),
        }



def _transition_pair(
    transition: StateTransition,
    observations_by_id: dict[str, MemoryObservation],
) -> tuple[MemoryObservation, MemoryObservation | None]:
    target = observations_by_id.get(transition.to_observation_id)
    if target is None:
        raise HandoffBuildError(f"transition target observation is missing: {transition.to_observation_id}")
    if transition.claim_key != target.claim_key or transition.scope_key != target.scope_key:
        raise HandoffBuildError("transition claim/scope does not match its target observation")

    prior: MemoryObservation | None = None
    if transition.from_observation_id is not None:
        prior = observations_by_id.get(transition.from_observation_id)
        if prior is None:
            raise HandoffBuildError(f"transition source observation is missing: {transition.from_observation_id}")
        if (
            prior.conversation_id != target.conversation_id
            or prior.scope_key != target.scope_key
            or prior.claim_key != target.claim_key
        ):
            raise HandoffBuildError("transition crosses conversation, scope, or claim boundaries")

    if transition.transition_kind in {"supersedes", "unchanged"} and prior is None:
        raise HandoffBuildError(f"{transition.transition_kind} transition requires a prior observation")
    return target, prior


def _operation_id(
    *,
    workspace_id: str,
    project_id: str,
    domain_profile: str,
    profile_version: str,
    source_id: str,
    observation: MemoryObservation,
    transition: StateTransition,
) -> str:
    return _hash_id(
        "op_",
        {
            "workspace_id": workspace_id,
            "project_id": project_id,
            "domain_profile": domain_profile,
            "profile_version": profile_version,
            "source_id": source_id,
            "conversation_id": observation.conversation_id,
            "claim_key": observation.claim_key,
            "scope_key": observation.scope_key,
            "observation_id": observation.observation_id,
            "transition_id": transition.transition_id,
            "transition_kind": transition.transition_kind,
        },
    )


def _source_scope(observation: MemoryObservation) -> HandoffSourceScope:
    return HandoffSourceScope(
        scope_type="conversation_observation",
        ref=observation.observation_id,
        start=observation.source_sequence,
        end=observation.source_sequence,
        locator_json={
            "conversation_id": observation.conversation_id,
            "claim_key": observation.claim_key,
            "scope_key": observation.scope_key,
            "source_sequence": observation.source_sequence,
        },
    )


def _write_item(
    *,
    observation: MemoryObservation,
    transition: StateTransition,
    prior: MemoryObservation | None,
    workspace_id: str,
    project_id: str,
    domain_profile: str,
    profile_version: str,
    source_id: str,
) -> MemoryWriteHandoffItem:
    if observation.state_kind not in WRITABLE_STATE_KINDS:
        raise HandoffBuildError(f"state kind is not writable: {observation.state_kind}")
    if transition.transition_kind not in {"new", "supersedes"}:
        raise HandoffBuildError("write items require new or supersedes transitions")

    evidences = _merge_evidences([observation, transition])
    primary_evidence = _primary_evidence(observation)
    operation_id = _operation_id(
        workspace_id=workspace_id,
        project_id=project_id,
        domain_profile=domain_profile,
        profile_version=profile_version,
        source_id=source_id,
        observation=observation,
        transition=transition,
    )
    values = {
        "operation_id": operation_id,
        "workspace_id": workspace_id,
        "project_id": project_id,
        "task_id": None,
        "conversation_id": observation.conversation_id,
        "domain_profile": domain_profile,
        "profile_version": profile_version,
        "memory_type": observation.memory_type,
        "subtype": observation.state_kind,
        "subject": None,
        "entity_type": None,
        "entity_id": None,
        "content": observation.content,
        "importance": "medium",
        "declared_sensitivity": "private",
        "requested_visibility": "private",
        "assertion_source": observation.assertion_source,
        "verification_state": observation.verification_state,
        "effective_from": observation.effective_from,
        "effective_to": observation.effective_to,
        "source_id": source_id,
        "source_unit_id": primary_evidence.source_unit_id,
        "source_scope": _source_scope(observation).to_dict(),
        "confidence": observation.confidence,
        "supersedes_ref": prior.observation_id if prior is not None else None,
        "supersedes_id": None,
        "expected_version": None,
        "domain_payload": {
            "memory_observation_id": observation.observation_id,
            "state_kind": observation.state_kind,
            "epistemic_status": observation.epistemic_status,
            "source_sequence": observation.source_sequence,
            "transition_reason": transition.reason,
        },
        "evidences": [item.to_dict() for item in evidences],
        "claim_key": observation.claim_key,
        "scope_key": observation.scope_key,
        "observed_at": observation.observed_at,
        "transition_kind": transition.transition_kind,
    }
    return materialize_idempotency_key(values)


def _pending_from_transition(
    *,
    transition: StateTransition,
    observation: MemoryObservation,
    prior: MemoryObservation | None,
) -> PendingReviewItem:
    related = {prior.observation_id} if prior is not None else set()
    evidences = _merge_evidences([observation, transition])
    material = {
        "transition_id": transition.transition_id,
        "to_observation_id": observation.observation_id,
        "from_observation_id": prior.observation_id if prior is not None else None,
        "transition_kind": transition.transition_kind,
        "reason": transition.reason,
        "evidence_ids": [item.evidence_id for item in evidences],
    }
    return PendingReviewItem(
        review_item_id=_hash_id("review_", material),
        claim_key=observation.claim_key,
        scope_key=observation.scope_key,
        reason=transition.reason,
        transition_kind=transition.transition_kind,
        evidences=evidences,
        observation_id=observation.observation_id,
        conversation_id=observation.conversation_id,
        content=observation.content,
        related_observation_ids=tuple(sorted(related)),
        review_status="needs_review",
        suggested_action="manual_review_required",
    )


def _orphan_pending(observation: MemoryObservation) -> PendingReviewItem:
    material = {
        "observation_id": observation.observation_id,
        "claim_key": observation.claim_key,
        "scope_key": observation.scope_key,
        "conversation_id": observation.conversation_id,
        "evidence_ids": [item.evidence_id for item in observation.evidences],
    }
    return PendingReviewItem(
        review_item_id=_hash_id("review_orphan_", material),
        claim_key=observation.claim_key,
        scope_key=observation.scope_key,
        reason="Observation has no matching state transition and cannot be written safely.",
        transition_kind="needs_review",
        evidences=observation.evidences,
        observation_id=observation.observation_id,
        conversation_id=observation.conversation_id,
        content=observation.content,
        related_observation_ids=(),
        review_status="needs_review",
        suggested_action="manual_review_required",
    )


def build_handoff(
    observations: Iterable[MemoryObservation],
    transitions: Iterable[StateTransition],
    workspace_id: str,
    project_id: str,
    domain_profile: str,
    profile_version: str,
    source_id: str,
) -> HandoffBuildResult:
    """Build deterministic database handoff suggestions without side effects."""

    for field_name, value in (
        ("workspace_id", workspace_id),
        ("project_id", project_id),
        ("domain_profile", domain_profile),
        ("profile_version", profile_version),
        ("source_id", source_id),
    ):
        if not isinstance(value, str) or not value.strip():
            raise HandoffBuildError(f"{field_name} must be a non-empty string")

    observation_list = tuple(observations)
    transition_list = tuple(transitions)
    observations_by_id: dict[str, MemoryObservation] = {}
    for observation in observation_list:
        if not isinstance(observation, MemoryObservation):
            raise HandoffBuildError("observations must contain MemoryObservation instances")
        if observation.observation_id in observations_by_id:
            raise HandoffBuildError(f"duplicate observation_id: {observation.observation_id}")
        observations_by_id[observation.observation_id] = observation

    transitions_by_target: dict[str, StateTransition] = {}
    for transition in transition_list:
        if not isinstance(transition, StateTransition):
            raise HandoffBuildError("transitions must contain StateTransition instances")
        if transition.to_observation_id not in observations_by_id:
            raise HandoffBuildError(f"transition target observation is missing: {transition.to_observation_id}")
        if transition.to_observation_id in transitions_by_target:
            raise HandoffBuildError(f"multiple transitions target observation: {transition.to_observation_id}")
        transitions_by_target[transition.to_observation_id] = transition

    write_items: list[MemoryWriteHandoffItem] = []
    pending_reviews: list[PendingReviewItem] = []
    skipped: list[SkippedObservation] = []
    write_order: dict[str, tuple[str, int, str]] = {}
    pending_order: dict[str, tuple[str, int, str]] = {}

    superseded_ids = {
        transition.from_observation_id
        for transition in transition_list
        if transition.transition_kind == "supersedes" and transition.from_observation_id is not None
    }

    for observation in sorted(observation_list, key=_observation_sort_key):
        transition = transitions_by_target.get(observation.observation_id)
        if transition is None:
            pending = _orphan_pending(observation)
            pending_reviews.append(pending)
            pending_order[pending.review_item_id] = _observation_sort_key(observation)
            continue

        target, prior = _transition_pair(transition, observations_by_id)
        if target.observation_id != observation.observation_id:
            raise HandoffBuildError("transition target mismatch")

        if transition.transition_kind in PENDING_TRANSITION_KINDS:
            pending = _pending_from_transition(transition=transition, observation=target, prior=prior)
            pending_reviews.append(pending)
            pending_order[pending.review_item_id] = _observation_sort_key(observation)
            continue

        if observation.observation_id in superseded_ids:
            skipped.append(
                SkippedObservation(
                    observation_id=observation.observation_id,
                    conversation_id=observation.conversation_id,
                    source_sequence=observation.source_sequence,
                    transition_id=transition.transition_id,
                    transition_kind=transition.transition_kind,
                    reason="Observation was superseded before the final handoff batch.",
                )
            )
            continue

        if transition.transition_kind in {"unchanged", "historical_only"}:
            skipped.append(
                SkippedObservation(
                    observation_id=observation.observation_id,
                    conversation_id=observation.conversation_id,
                    source_sequence=observation.source_sequence,
                    transition_id=transition.transition_id,
                    transition_kind=transition.transition_kind,
                    reason="Transition does not represent a new current database write.",
                )
            )
            continue

        if transition.transition_kind in {"new", "supersedes"}:
            if observation.state_kind not in WRITABLE_STATE_KINDS:
                pending = _orphan_pending(observation)
                pending_reviews.append(pending)
                pending_order[pending.review_item_id] = _observation_sort_key(observation)
                continue
            item = _write_item(
                observation=observation,
                transition=transition,
                prior=prior,
                workspace_id=workspace_id,
                project_id=project_id,
                domain_profile=domain_profile,
                profile_version=profile_version,
                source_id=source_id,
            )
            write_items.append(item)
            write_order[item.idempotency_key] = _observation_sort_key(observation)
            continue

        raise HandoffBuildError(f"unsupported transition kind: {transition.transition_kind}")

    write_items.sort(key=lambda item: (*write_order[item.idempotency_key], item.idempotency_key))
    pending_reviews.sort(key=lambda item: (*pending_order[item.review_item_id], item.review_item_id))
    skipped.sort(
        key=lambda item: (
            item.conversation_id,
            item.source_sequence,
            item.observation_id,
            item.transition_id,
            item.reason,
        )
    )

    counts = HandoffBuildCounts(
        observations=len(observation_list),
        transitions=len(transition_list),
        write_items=len(write_items),
        pending_review_items=len(pending_reviews),
        skipped_observations=len(skipped),
    )
    return HandoffBuildResult(
        write_items=tuple(write_items),
        pending_review_items=tuple(pending_reviews),
        skipped_observations=tuple(skipped),
        counts=counts,
    )


build_database_handoff = build_handoff
