from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from itertools import groupby
from typing import Any

from .handoff_contracts import (
    ASSERTION_SOURCES,
    MEMORY_TYPES,
    REVIEW_STATUSES,
    VERIFICATION_STATES,
    HandoffEvidence,
    MemoryObservation,
    PendingReviewItem,
    StateTransition,
)


SUPERSEDING_STATE_KINDS = frozenset({"decision", "fact", "result"})
NON_OVERRIDING_STATE_KINDS = frozenset({"discussion", "hypothesis", "task"})
CURRENT_STATE_KINDS = frozenset({"decision", "fact", "result", "preference", "constraint"})
STATE_PRIORITY = {
    "result": 5,
    "fact": 4,
    "decision": 3,
    "constraint": 2,
    "preference": 2,
    "task": 1,
    "discussion": 0,
    "hypothesis": 0,
    "conflict": 0,
}
_AMBIGUOUS = re.compile(
    r"[?？]|要不要|是否|可能|也许|不确定|待定|如果|假设|maybe|might|possibly|not sure",
    re.IGNORECASE,
)
_QUESTION = re.compile(r"[?？]|要不要|是否|为什么|如何|怎么|哪一|哪些", re.IGNORECASE)
_MISSING = object()
_NO_DEFAULT = object()


class StateResolutionError(ValueError):
    """Raised when state observations cannot be resolved safely."""


@dataclass(frozen=True)
class StateResolutionResult:
    observations: tuple[MemoryObservation, ...]
    transitions: tuple[StateTransition, ...]
    pending_review_items: tuple[PendingReviewItem, ...]
    current_observation_ids: tuple[str, ...]
    historical_observation_ids: tuple[str, ...]
    superseded_observation_ids: tuple[str, ...]
    conflict_observation_ids: tuple[str, ...]
    needs_review_observation_ids: tuple[str, ...]

    @property
    def pending_reviews(self) -> tuple[PendingReviewItem, ...]:
        return self.pending_review_items

    def to_dict(self) -> dict[str, Any]:
        return {
            "observations": [item.to_dict() for item in self.observations],
            "transitions": [item.to_dict() for item in self.transitions],
            "pending_review_items": [item.to_dict() for item in self.pending_review_items],
            "current_observation_ids": list(self.current_observation_ids),
            "historical_observation_ids": list(self.historical_observation_ids),
            "superseded_observation_ids": list(self.superseded_observation_ids),
            "conflict_observation_ids": list(self.conflict_observation_ids),
            "needs_review_observation_ids": list(self.needs_review_observation_ids),
        }


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash_id(prefix: str, value: Any) -> str:
    return prefix + hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()[:24]


def _field(value: Any, name: str, default: Any = _NO_DEFAULT) -> Any:
    if isinstance(value, Mapping):
        if name in value:
            return value[name]
    elif hasattr(value, name):
        return getattr(value, name)
    if default is _NO_DEFAULT:
        raise StateResolutionError(f"candidate is missing {name}")
    return default


def _normalized_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().casefold()


def _is_ambiguous(text: str) -> bool:
    return bool(_AMBIGUOUS.search(text))


def _looks_question(text: str) -> bool:
    return bool(_QUESTION.search(text))


def build_scope_key(
    *,
    workspace_id: str,
    project_id: str,
    subject: str | None = None,
    entity_type: str | None = None,
    entity_id: str | None = None,
) -> str:
    """Build a deterministic, collision-resistant logical scope key."""

    parts = [("workspace", workspace_id), ("project", project_id)]
    if subject:
        parts.append(("subject", subject))
    if entity_type:
        parts.append(("entity_type", entity_type))
    if entity_id:
        parts.append(("entity_id", entity_id))
    encoded: list[str] = []
    for name, value in parts:
        if not isinstance(value, str) or not value.strip():
            raise StateResolutionError(f"scope component {name} must be a non-empty string")
        encoded.append(f"{name}:{len(value)}:{value}")
    return "|".join(encoded)


def _candidate_evidence(value: Any) -> HandoffEvidence:
    if isinstance(value, HandoffEvidence):
        return value
    if not isinstance(value, Mapping):
        raw = {
            "evidence_id": _field(value, "evidence_id"),
            "source_unit_id": _field(value, "source_unit_id"),
            "quote": _field(value, "quote"),
            "status": _field(value, "status", "unresolved"),
        }
        locator = _field(value, "locator_json", None)
        if locator is None:
            locator = {
                key: _field(value, key, None)
                for key in ("message_id", "char_start", "char_end", "source_unit_hash")
                if _field(value, key, None) is not None
            }
        raw["locator_json"] = locator
        value = raw
    status = _field(value, "status", "unresolved")
    if status not in {"verified", "rejected", "unresolved"}:
        status = "unresolved"
    return HandoffEvidence.from_dict(
        {
            "evidence_id": _field(value, "evidence_id"),
            "source_unit_id": _field(value, "source_unit_id"),
            "quote": _field(value, "quote"),
            "locator_json": _field(value, "locator_json", {}),
            "status": status,
        }
    )


def _candidate_evidences(candidate: Any) -> tuple[HandoffEvidence, ...]:
    raw = _field(candidate, "evidences", _MISSING)
    if raw is _MISSING:
        raw = _field(candidate, "evidence", _MISSING)
    if raw is _MISSING:
        raise StateResolutionError("candidate is missing evidence/evidences")
    if isinstance(raw, (str, bytes, bytearray)) or not isinstance(raw, Sequence):
        raise StateResolutionError("candidate evidence must be a sequence")
    evidences = tuple(_candidate_evidence(item) for item in raw)
    if not evidences:
        raise StateResolutionError("candidate must contain at least one evidence item")
    return evidences


def _memory_type_for_candidate(candidate_type: str) -> str:
    mapping = {
        "requirement": "requirement",
        "constraint": "constraint",
        "decision": "decision",
        "fact": "fact",
        "task": "task",
        "preference": "preference",
        "open_question": "open_question",
        "solution": "solution",
        "artifact": "artifact",
        "relationship": "relationship",
        "event": "event",
    }
    return mapping.get(candidate_type, "fact")


def _state_kind_for_candidate(candidate_type: str, content: str) -> str:
    if _looks_question(content):
        return "discussion"
    if _is_ambiguous(content):
        return "hypothesis"
    mapping = {
        "decision": "decision",
        "fact": "fact",
        "result": "result",
        "task": "task",
        "preference": "preference",
        "constraint": "constraint",
        "requirement": "constraint",
        "conflict": "conflict",
        "open_question": "discussion",
        "discussion": "discussion",
        "hypothesis": "hypothesis",
    }
    return mapping.get(candidate_type, "discussion")


def candidate_to_observation(
    candidate: Any,
    *,
    observed_at: str | None = None,
    source_sequence: int | None = None,
    scope_key: str | None = None,
    claim_key: str | None = None,
    state_kind: str | None = None,
    memory_type: str | None = None,
    assertion_source: str | None = None,
    verification_state: str | None = None,
    epistemic_status: str | None = None,
    confidence: float | None = None,
    effective_from: str | None = None,
    effective_to: str | None = None,
    review_status: str | None = None,
    observation_id: str | None = None,
) -> MemoryObservation:
    """Purely convert a candidate-like object into a strict MemoryObservation."""

    conversation_id = _field(candidate, "conversation_id")
    content = _field(candidate, "content", _MISSING)
    if content is _MISSING:
        content = _field(candidate, "text")
    candidate_id = _field(candidate, "candidate_id", _field(candidate, "group_id", None))
    resolved_claim_key = claim_key or _field(candidate, "claim_key", None)
    if not resolved_claim_key:
        if not candidate_id:
            raise StateResolutionError("claim_key must be supplied when candidate_id is absent")
        resolved_claim_key = f"candidate:{candidate_id}"
    resolved_scope_key = scope_key or _field(candidate, "scope_key", None)
    if not resolved_scope_key:
        resolved_scope_key = build_scope_key(
            workspace_id=_field(candidate, "workspace_id"),
            project_id=_field(candidate, "project_id"),
            subject=_field(candidate, "subject", None),
            entity_type=_field(candidate, "entity_type", None),
            entity_id=_field(candidate, "entity_id", None),
        )
    resolved_observed_at = observed_at or _field(candidate, "observed_at", None)
    if not resolved_observed_at:
        raise StateResolutionError("observed_at must be supplied explicitly")
    resolved_source_sequence = source_sequence
    if resolved_source_sequence is None:
        resolved_source_sequence = _field(candidate, "source_sequence", None)
    if resolved_source_sequence is None:
        raise StateResolutionError("source_sequence must be supplied explicitly")
    candidate_type = str(_field(candidate, "candidate_type", _field(candidate, "record_status", "discussion")))
    evidences = _candidate_evidences(candidate)
    resolved_memory_type = memory_type or _field(candidate, "proposed_memory_type", None)
    if resolved_memory_type not in MEMORY_TYPES:
        resolved_memory_type = _memory_type_for_candidate(candidate_type)
    resolved_state_kind = state_kind or _field(candidate, "state_kind", None)
    if resolved_state_kind is None:
        resolved_state_kind = _state_kind_for_candidate(candidate_type, str(content))
    resolved_assertion_source = assertion_source or _field(candidate, "assertion_source", None)
    if resolved_assertion_source not in ASSERTION_SOURCES:
        resolved_assertion_source = "user_report"
    resolved_verification_state = verification_state or _field(candidate, "verification_state", None)
    if resolved_verification_state not in VERIFICATION_STATES:
        resolved_verification_state = "unverified"
    resolved_epistemic_status = epistemic_status or _field(candidate, "epistemic_status", None) or "unspecified"
    resolved_confidence = confidence if confidence is not None else _field(candidate, "confidence", 0.5)
    resolved_review_status = review_status or _field(candidate, "review_status", "pending_review")
    if resolved_review_status not in REVIEW_STATUSES:
        resolved_review_status = "pending_review"
    resolved_effective_from = effective_from or _field(candidate, "effective_from", None)
    resolved_effective_to = effective_to or _field(candidate, "effective_to", None)
    if observation_id is None:
        observation_id = _hash_id(
            "obs_",
            {
                "conversation_id": conversation_id,
                "source_sequence": resolved_source_sequence,
                "claim_key": resolved_claim_key,
                "scope_key": resolved_scope_key,
                "observed_at": resolved_observed_at,
                "content": content,
                "source_unit_ids": sorted(item.source_unit_id for item in evidences),
            },
        )
    return MemoryObservation(
        observation_id=observation_id,
        conversation_id=conversation_id,
        source_sequence=resolved_source_sequence,
        observed_at=resolved_observed_at,
        claim_key=resolved_claim_key,
        scope_key=resolved_scope_key,
        memory_type=resolved_memory_type,
        content=str(content),
        assertion_source=resolved_assertion_source,
        verification_state=resolved_verification_state,
        epistemic_status=resolved_epistemic_status,
        state_kind=resolved_state_kind,
        evidences=evidences,
        confidence=resolved_confidence,
        effective_from=resolved_effective_from,
        effective_to=resolved_effective_to,
        review_status=resolved_review_status,
    )


observation_from_candidate = candidate_to_observation


def _observation_time(value: MemoryObservation) -> datetime:
    return datetime.fromisoformat(value.observed_at.replace("Z", "+00:00")).astimezone(timezone.utc)


def _observation_sort_key(value: MemoryObservation) -> tuple[datetime, int, str]:
    return (_observation_time(value), value.source_sequence, value.observation_id)


def _state_priority(state_kind: str) -> int:
    return STATE_PRIORITY[state_kind]


def _semantic_signature(value: MemoryObservation) -> tuple[str, str]:
    return (value.state_kind, _normalized_text(value.content))


def _merge_evidences(observations: Iterable[MemoryObservation]) -> tuple[HandoffEvidence, ...]:
    by_id: dict[str, HandoffEvidence] = {}
    for observation in observations:
        for evidence in observation.evidences:
            existing = by_id.get(evidence.evidence_id)
            if existing is not None and existing.to_dict() != evidence.to_dict():
                raise StateResolutionError(f"evidence_id {evidence.evidence_id} has conflicting payloads")
            by_id[evidence.evidence_id] = evidence
    return tuple(by_id[key] for key in sorted(by_id))


def _make_transition(
    *,
    current: MemoryObservation | None,
    observation: MemoryObservation,
    transition_kind: str,
    reason: str,
    involved: Sequence[MemoryObservation],
    from_observation_id: str | None = None,
) -> StateTransition:
    effective_from = from_observation_id
    if effective_from is None and current is not None and transition_kind != "new":
        effective_from = current.observation_id
    evidences = _merge_evidences(involved)
    material = {
        "from_observation_id": effective_from,
        "to_observation_id": observation.observation_id,
        "transition_kind": transition_kind,
        "reason": reason,
        "claim_key": observation.claim_key,
        "scope_key": observation.scope_key,
        "conversation_id": observation.conversation_id,
        "evidence_ids": [item.evidence_id for item in evidences],
    }
    return StateTransition(
        transition_id=_hash_id("tr_", material),
        claim_key=observation.claim_key,
        scope_key=observation.scope_key,
        to_observation_id=observation.observation_id,
        transition_kind=transition_kind,
        reason=reason,
        evidences=evidences,
        from_observation_id=effective_from,
        confidence=observation.confidence,
        review_status="needs_review" if transition_kind in {"conflict", "needs_review"} else "pending_review",
    )


def _make_pending_review(
    *,
    current: MemoryObservation | None,
    observation: MemoryObservation,
    transition_kind: str,
    reason: str,
    involved: Sequence[MemoryObservation],
) -> PendingReviewItem:
    related = {item.observation_id for item in involved if item.observation_id != observation.observation_id}
    if current is not None and current.observation_id != observation.observation_id:
        related.add(current.observation_id)
    evidences = _merge_evidences(involved)
    related_ids = tuple(sorted(related))
    material = {
        "claim_key": observation.claim_key,
        "scope_key": observation.scope_key,
        "conversation_id": observation.conversation_id,
        "observation_id": observation.observation_id,
        "related_observation_ids": related_ids,
        "transition_kind": transition_kind,
        "reason": reason,
        "evidence_ids": [item.evidence_id for item in evidences],
    }
    return PendingReviewItem(
        review_item_id=_hash_id("review_", material),
        claim_key=observation.claim_key,
        scope_key=observation.scope_key,
        reason=reason,
        transition_kind=transition_kind,
        evidences=evidences,
        observation_id=observation.observation_id,
        conversation_id=observation.conversation_id,
        content=observation.content,
        related_observation_ids=related_ids,
        review_status="needs_review",
        suggested_action="manual_review_required",
    )


def resolve_state_transitions(observations: Iterable[MemoryObservation]) -> StateResolutionResult:
    """Resolve deterministic conversation-state transitions without side effects."""

    observation_list = tuple(observations)
    seen_ids: set[str] = set()
    for observation in observation_list:
        if not isinstance(observation, MemoryObservation):
            raise StateResolutionError("resolver accepts MemoryObservation instances only")
        if observation.observation_id in seen_ids:
            raise StateResolutionError(f"duplicate observation_id: {observation.observation_id}")
        seen_ids.add(observation.observation_id)

    sorted_observations = tuple(sorted(observation_list, key=_observation_sort_key))
    grouped: dict[tuple[str, str, str], list[MemoryObservation]] = {}
    for observation in sorted_observations:
        key = (observation.scope_key, observation.claim_key, observation.conversation_id)
        grouped.setdefault(key, []).append(observation)

    transitions: list[StateTransition] = []
    pending_reviews: list[PendingReviewItem] = []
    current_ids: list[str] = []

    for group_key in sorted(grouped):
        chain = sorted(grouped[group_key], key=_observation_sort_key)
        current: MemoryObservation | None = None
        for _, bucket_iter in groupby(chain, key=lambda item: _observation_time(item).isoformat()):
            bucket = tuple(bucket_iter)
            all_involved = ((current,) if current is not None else ()) + bucket
            explicit_conflicts = tuple(item for item in bucket if item.state_kind == "conflict")

            if explicit_conflicts:
                reason = "A conflict observation requires manual review; no state was overwritten."
                for observation in bucket:
                    kind = "conflict" if observation.state_kind == "conflict" else "needs_review"
                    transitions.append(_make_transition(current=current, observation=observation, transition_kind=kind, reason=reason, involved=all_involved))
                    pending_reviews.append(_make_pending_review(current=current, observation=observation, transition_kind=kind, reason=reason, involved=all_involved))
                continue

            non_overriding = tuple(item for item in bucket if item.state_kind in NON_OVERRIDING_STATE_KINDS)
            active = tuple(item for item in bucket if item.state_kind not in NON_OVERRIDING_STATE_KINDS)

            if not active:
                reason = "Discussion, hypothesis, or task observation does not replace current state."
                for observation in non_overriding:
                    transitions.append(_make_transition(current=current, observation=observation, transition_kind="historical_only", reason=reason, involved=all_involved if current is not None else (observation,)))
                continue

            highest_priority = max(_state_priority(item.state_kind) for item in active)
            top = tuple(item for item in active if _state_priority(item.state_kind) == highest_priority)
            lower = tuple(item for item in bucket if item not in top)
            signatures = {_semantic_signature(item) for item in top}

            if len(signatures) > 1:
                reason = "Multiple same-time observations have equal state priority but different content."
                for observation in top:
                    transitions.append(_make_transition(current=current, observation=observation, transition_kind="conflict", reason=reason, involved=all_involved))
                    pending_reviews.append(_make_pending_review(current=current, observation=observation, transition_kind="conflict", reason=reason, involved=all_involved))
                for observation in lower:
                    transitions.append(_make_transition(current=current, observation=observation, transition_kind="historical_only", reason="Lower-priority same-time observation was not selected.", involved=all_involved if current is not None else (observation,)))
                continue

            canonical = min(top, key=lambda item: (item.source_sequence, item.observation_id))
            duplicates = tuple(item for item in top if item.observation_id != canonical.observation_id)
            top_is_ambiguous = any(_is_ambiguous(item.content) for item in top)

            if top_is_ambiguous:
                reason = "Ambiguous or uncertain later statement requires review and cannot overwrite current state."
                transitions.append(_make_transition(current=current, observation=canonical, transition_kind="needs_review", reason=reason, involved=all_involved))
                pending_reviews.append(_make_pending_review(current=current, observation=canonical, transition_kind="needs_review", reason=reason, involved=all_involved))
                for duplicate in duplicates:
                    transitions.append(_make_transition(current=current, observation=duplicate, transition_kind="unchanged", reason="Duplicate same-time observation matches the review candidate.", involved=(canonical, duplicate), from_observation_id=canonical.observation_id))
                for observation in lower:
                    transitions.append(_make_transition(current=current, observation=observation, transition_kind="historical_only", reason="Lower-priority same-time observation was not selected.", involved=all_involved if current is not None else (observation,)))
                continue

            if current is None:
                transitions.append(_make_transition(current=None, observation=canonical, transition_kind="new", reason="First authoritative observation in this conversation state chain.", involved=(canonical,)))
                current = canonical
            elif canonical.state_kind == current.state_kind and _normalized_text(canonical.content) == _normalized_text(current.content):
                transitions.append(_make_transition(current=current, observation=canonical, transition_kind="unchanged", reason="Later observation restates the current state.", involved=(current, canonical)))
            elif canonical.state_kind in SUPERSEDING_STATE_KINDS:
                transitions.append(_make_transition(current=current, observation=canonical, transition_kind="supersedes", reason=f"Later explicit {canonical.state_kind} supersedes the current state.", involved=(current, canonical)))
                current = canonical
            else:
                reason = "Later statement is not an allowed automatic superseding state."
                transitions.append(_make_transition(current=current, observation=canonical, transition_kind="needs_review", reason=reason, involved=(current, canonical)))
                pending_reviews.append(_make_pending_review(current=current, observation=canonical, transition_kind="needs_review", reason=reason, involved=(current, canonical)))

            for duplicate in duplicates:
                transitions.append(_make_transition(current=current, observation=duplicate, transition_kind="unchanged", reason="Duplicate same-time observation matches the selected observation.", involved=(canonical, duplicate), from_observation_id=canonical.observation_id))
            for observation in lower:
                transitions.append(_make_transition(current=current, observation=observation, transition_kind="historical_only", reason="Lower-priority same-time observation was not selected.", involved=all_involved if current is not None else (observation,)))

        if current is not None:
            current_ids.append(current.observation_id)

    transitions.sort(key=lambda item: (item.scope_key, item.claim_key, item.to_observation_id, item.transition_kind))
    pending_reviews.sort(key=lambda item: item.review_item_id)
    filtered_current = tuple(sorted(set(current_ids)))
    historical = tuple(sorted({item.to_observation_id for item in transitions if item.transition_kind == "historical_only"}))
    superseded = tuple(sorted({item.from_observation_id for item in transitions if item.transition_kind == "supersedes" and item.from_observation_id is not None}))
    conflicts = tuple(sorted({item.to_observation_id for item in transitions if item.transition_kind == "conflict"}))
    needs_review = tuple(sorted({item.to_observation_id for item in transitions if item.transition_kind == "needs_review"}))
    return StateResolutionResult(
        observations=sorted_observations,
        transitions=tuple(transitions),
        pending_review_items=tuple(pending_reviews),
        current_observation_ids=filtered_current,
        historical_observation_ids=historical,
        superseded_observation_ids=superseded,
        conflict_observation_ids=conflicts,
        needs_review_observation_ids=needs_review,
    )


resolve_observations = resolve_state_transitions
resolve_conversation_states = resolve_state_transitions
