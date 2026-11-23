from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


HANDOFF_CONTRACT_VERSION = "conversation-memory-handoff-v1"
TRANSITION_KINDS = (
    "new",
    "unchanged",
    "supersedes",
    "conflict",
    "needs_review",
    "historical_only",
)
STATE_KINDS = (
    "discussion",
    "hypothesis",
    "decision",
    "fact",
    "task",
    "result",
    "preference",
    "constraint",
    "conflict",
)
MEMORY_TYPES = (
    "fact",
    "requirement",
    "constraint",
    "decision",
    "task",
    "procedure",
    "issue",
    "solution",
    "artifact",
    "relationship",
    "event",
    "preference",
    "open_question",
)
IMPORTANCE_LEVELS = ("critical", "high", "medium", "low")
SENSITIVITY_LEVELS = ("public", "internal", "private", "secret")
VISIBILITY_LEVELS = ("private", "workspace")
ASSERTION_SOURCES = (
    "user_decision",
    "user_preference",
    "user_request",
    "user_report",
    "primary_source",
    "external_document",
    "tool_output",
    "assistant_proposal",
    "assistant_inference",
    "legacy_unknown",
)
VERIFICATION_STATES = (
    "not_required",
    "quote_verified",
    "user_confirmed",
    "source_verified",
    "tool_verified",
    "unverified",
    "conflict",
    "legacy_unclassified",
)
EVIDENCE_STATUSES = ("verified", "rejected", "unresolved")
REVIEW_STATUSES = ("pending_review", "needs_review", "accepted", "rejected")
PENDING_TRANSITION_KINDS = ("conflict", "needs_review")


class HandoffContractError(ValueError):
    """Raised when a handoff record violates the v1 data contract."""


def _as_mapping(value: Any, record: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise HandoffContractError(f"{record} must be a mapping")
    return dict(value)


def _check_fields(data: Mapping[str, Any], *, record: str, allowed: frozenset[str], required: tuple[str, ...]) -> None:
    unknown = set(data) - allowed
    if unknown:
        raise HandoffContractError(f"{record} contains unknown fields: {', '.join(sorted(unknown))}")
    missing = [name for name in required if name not in data]
    if missing:
        raise HandoffContractError(f"{record} is missing required fields: {', '.join(missing)}")


def _text(value: Any, field_name: str, *, optional: bool = False, allow_empty: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise HandoffContractError(f"{field_name} must be a string")
    if not allow_empty and not value.strip():
        raise HandoffContractError(f"{field_name} must not be empty")
    return value


def _optional_text(value: Any, field_name: str) -> str | None:
    return _text(value, field_name, optional=True)


def _boolean(value: Any, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise HandoffContractError(f"{field_name} must be a boolean")
    return value


def _integer(value: Any, field_name: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise HandoffContractError(f"{field_name} must be an integer")
    if minimum is not None and value < minimum:
        raise HandoffContractError(f"{field_name} must be >= {minimum}")
    return value


def _confidence(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HandoffContractError("confidence must be a number")
    result = float(value)
    if not math.isfinite(result) or not 0.0 <= result <= 1.0:
        raise HandoffContractError("confidence must be between 0.0 and 1.0")
    return result


def _enum(value: Any, field_name: str, allowed: Sequence[str]) -> str:
    text = _text(value, field_name)
    assert text is not None
    if text not in allowed:
        raise HandoffContractError(f"{field_name} must be one of: {', '.join(allowed)}")
    return text


def _datetime_text(value: Any, field_name: str, *, optional: bool = False) -> str | None:
    text = _text(value, field_name, optional=optional)
    if text is None:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HandoffContractError(f"{field_name} must be RFC3339 date-time") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise HandoffContractError(f"{field_name} must include a timezone offset")
    return text


def _canonical_datetime(value: str | None) -> str | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _json_object(value: Any, field_name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise HandoffContractError(f"{field_name} must be a JSON object")
    result = _json_value(dict(value), field_name)
    assert isinstance(result, dict)
    return result


def _json_value(value: Any, field_name: str) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise HandoffContractError(f"{field_name} must contain finite numbers")
        return value
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise HandoffContractError(f"{field_name} object keys must be strings")
            result[key] = _json_value(item, field_name)
        return result
    if isinstance(value, (list, tuple)):
        return [_json_value(item, field_name) for item in value]
    raise HandoffContractError(f"{field_name} must contain only JSON-compatible values")


def _sequence(value: Any, field_name: str, *, nonempty: bool = False) -> tuple[Any, ...]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise HandoffContractError(f"{field_name} must be a sequence")
    items = tuple(value)
    if nonempty and not items:
        raise HandoffContractError(f"{field_name} must not be empty")
    return items


def _validate_effective_range(effective_from: str | None, effective_to: str | None) -> None:
    if not effective_from or not effective_to:
        return
    start = datetime.fromisoformat(effective_from.replace("Z", "+00:00"))
    end = datetime.fromisoformat(effective_to.replace("Z", "+00:00"))
    if end < start:
        raise HandoffContractError("effective_to must not precede effective_from")


def _unique_text_sequence(value: Any, field_name: str, *, nonempty: bool = False) -> tuple[str, ...]:
    items = tuple(_text(item, field_name) for item in _sequence(value, field_name, nonempty=nonempty))
    assert all(item is not None for item in items)
    if len(items) != len(set(items)):
        raise HandoffContractError(f"{field_name} must not contain duplicates")
    return items  # type: ignore[return-value]


@dataclass(frozen=True)
class HandoffEvidence:
    evidence_id: str
    source_unit_id: str
    quote: str
    locator_json: dict[str, Any] = field(default_factory=dict)
    status: str = "unresolved"

    _FIELDS = frozenset({"evidence_id", "source_unit_id", "quote", "locator_json", "status"})
    _REQUIRED = ("evidence_id", "source_unit_id", "quote")

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence_id", _text(self.evidence_id, "evidence_id"))
        object.__setattr__(self, "source_unit_id", _text(self.source_unit_id, "source_unit_id"))
        object.__setattr__(self, "quote", _text(self.quote, "quote"))
        object.__setattr__(self, "locator_json", _json_object(self.locator_json, "locator_json"))
        object.__setattr__(self, "status", _enum(self.status, "status", EVIDENCE_STATUSES))

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "source_unit_id": self.source_unit_id,
            "quote": self.quote,
            "locator_json": _json_value(self.locator_json, "locator_json"),
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "HandoffEvidence":
        data = _as_mapping(value, cls.__name__)
        _check_fields(data, record=cls.__name__, allowed=cls._FIELDS, required=cls._REQUIRED)
        return cls(
            evidence_id=data["evidence_id"], source_unit_id=data["source_unit_id"], quote=data["quote"],
            locator_json=data.get("locator_json", {}), status=data.get("status", "unresolved"),
        )


@dataclass(frozen=True)
class HandoffSourceScope:
    scope_type: str
    ref: str
    start: str | int | None = None
    end: str | int | None = None
    locator_json: dict[str, Any] = field(default_factory=dict)

    _FIELDS = frozenset({"scope_type", "ref", "start", "end", "locator_json"})
    _REQUIRED = ("scope_type", "ref")

    def __post_init__(self) -> None:
        object.__setattr__(self, "scope_type", _text(self.scope_type, "scope_type"))
        object.__setattr__(self, "ref", _text(self.ref, "ref"))
        for field_name in ("start", "end"):
            value = getattr(self, field_name)
            if value is not None and (isinstance(value, bool) or not isinstance(value, (str, int))):
                raise HandoffContractError(f"{field_name} must be a string, integer, or null")
        object.__setattr__(self, "locator_json", _json_object(self.locator_json, "locator_json"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope_type": self.scope_type,
            "ref": self.ref,
            "start": self.start,
            "end": self.end,
            "locator_json": _json_value(self.locator_json, "locator_json"),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "HandoffSourceScope":
        data = _as_mapping(value, cls.__name__)
        _check_fields(data, record=cls.__name__, allowed=cls._FIELDS, required=cls._REQUIRED)
        return cls(
            scope_type=data["scope_type"], ref=data["ref"], start=data.get("start"), end=data.get("end"),
            locator_json=data.get("locator_json", {}),
        )


def _coerce_evidence(value: HandoffEvidence | Mapping[str, Any]) -> HandoffEvidence:
    if isinstance(value, HandoffEvidence):
        return value
    if isinstance(value, Mapping):
        return HandoffEvidence.from_dict(value)
    raise HandoffContractError("evidences entries must be HandoffEvidence or mappings")


def _evidence_tuple(value: Any, *, nonempty: bool = True) -> tuple[HandoffEvidence, ...]:
    items = tuple(_coerce_evidence(item) for item in _sequence(value, "evidences", nonempty=nonempty))
    ids = [item.evidence_id for item in items]
    if len(ids) != len(set(ids)):
        raise HandoffContractError("evidences must not contain duplicate evidence_id values")
    return items


def _coerce_scope(value: HandoffSourceScope | Mapping[str, Any]) -> HandoffSourceScope:
    if isinstance(value, HandoffSourceScope):
        return value
    if isinstance(value, Mapping):
        return HandoffSourceScope.from_dict(value)
    raise HandoffContractError("source_scope must be HandoffSourceScope or a mapping")


@dataclass(frozen=True)
class MemoryObservation:
    observation_id: str
    conversation_id: str
    source_sequence: int
    observed_at: str
    claim_key: str
    scope_key: str
    memory_type: str
    content: str
    assertion_source: str
    verification_state: str
    epistemic_status: str
    state_kind: str
    evidences: tuple[HandoffEvidence, ...]
    confidence: float = 0.5
    effective_from: str | None = None
    effective_to: str | None = None
    review_status: str = "pending_review"

    _FIELDS = frozenset({
        "observation_id", "conversation_id", "source_sequence", "observed_at", "claim_key", "scope_key",
        "memory_type", "content", "assertion_source", "verification_state", "epistemic_status", "state_kind",
        "evidences", "confidence", "effective_from", "effective_to", "review_status",
    })
    _REQUIRED = (
        "observation_id", "conversation_id", "source_sequence", "observed_at", "claim_key", "scope_key",
        "memory_type", "content", "assertion_source", "verification_state", "epistemic_status", "state_kind", "evidences",
    )

    def __post_init__(self) -> None:
        for field_name in ("observation_id", "conversation_id", "claim_key", "scope_key", "content", "epistemic_status"):
            object.__setattr__(self, field_name, _text(getattr(self, field_name), field_name))
        object.__setattr__(self, "source_sequence", _integer(self.source_sequence, "source_sequence", minimum=0))
        object.__setattr__(self, "observed_at", _datetime_text(self.observed_at, "observed_at"))
        object.__setattr__(self, "memory_type", _enum(self.memory_type, "memory_type", MEMORY_TYPES))
        object.__setattr__(self, "assertion_source", _enum(self.assertion_source, "assertion_source", ASSERTION_SOURCES))
        object.__setattr__(self, "verification_state", _enum(self.verification_state, "verification_state", VERIFICATION_STATES))
        object.__setattr__(self, "state_kind", _enum(self.state_kind, "state_kind", STATE_KINDS))
        object.__setattr__(self, "evidences", _evidence_tuple(self.evidences))
        object.__setattr__(self, "confidence", _confidence(self.confidence))
        object.__setattr__(self, "effective_from", _datetime_text(self.effective_from, "effective_from", optional=True))
        object.__setattr__(self, "effective_to", _datetime_text(self.effective_to, "effective_to", optional=True))
        _validate_effective_range(self.effective_from, self.effective_to)
        object.__setattr__(self, "review_status", _enum(self.review_status, "review_status", REVIEW_STATUSES))

    def to_dict(self) -> dict[str, Any]:
        return {
            "observation_id": self.observation_id, "conversation_id": self.conversation_id,
            "source_sequence": self.source_sequence, "observed_at": self.observed_at,
            "claim_key": self.claim_key, "scope_key": self.scope_key, "memory_type": self.memory_type,
            "content": self.content, "assertion_source": self.assertion_source,
            "verification_state": self.verification_state, "epistemic_status": self.epistemic_status,
            "state_kind": self.state_kind, "evidences": [item.to_dict() for item in self.evidences],
            "confidence": self.confidence, "effective_from": self.effective_from,
            "effective_to": self.effective_to, "review_status": self.review_status,
        }

    @property
    def evidence_ids(self) -> tuple[str, ...]:
        return tuple(item.evidence_id for item in self.evidences)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "MemoryObservation":
        data = _as_mapping(value, cls.__name__)
        _check_fields(data, record=cls.__name__, allowed=cls._FIELDS, required=cls._REQUIRED)
        return cls(
            observation_id=data["observation_id"], conversation_id=data["conversation_id"],
            source_sequence=data["source_sequence"], observed_at=data["observed_at"],
            claim_key=data["claim_key"], scope_key=data["scope_key"], memory_type=data["memory_type"],
            content=data["content"], assertion_source=data["assertion_source"],
            verification_state=data["verification_state"], epistemic_status=data["epistemic_status"],
            state_kind=data["state_kind"], evidences=_evidence_tuple(data["evidences"]),
            confidence=data.get("confidence", 0.5), effective_from=data.get("effective_from"),
            effective_to=data.get("effective_to"), review_status=data.get("review_status", "pending_review"),
        )


@dataclass(frozen=True)
class StateTransition:
    transition_id: str
    claim_key: str
    scope_key: str
    to_observation_id: str
    transition_kind: str
    reason: str
    evidences: tuple[HandoffEvidence, ...]
    from_observation_id: str | None = None
    confidence: float = 0.5
    review_status: str = "pending_review"

    _FIELDS = frozenset({
        "transition_id", "claim_key", "scope_key", "to_observation_id", "transition_kind", "reason",
        "evidences", "from_observation_id", "confidence", "review_status",
    })
    _REQUIRED = ("transition_id", "claim_key", "scope_key", "to_observation_id", "transition_kind", "reason", "evidences")

    def __post_init__(self) -> None:
        for field_name in ("transition_id", "claim_key", "scope_key", "to_observation_id", "reason"):
            object.__setattr__(self, field_name, _text(getattr(self, field_name), field_name))
        object.__setattr__(self, "from_observation_id", _optional_text(self.from_observation_id, "from_observation_id"))
        object.__setattr__(self, "transition_kind", _enum(self.transition_kind, "transition_kind", TRANSITION_KINDS))
        if self.transition_kind == "new" and self.from_observation_id is not None:
            raise HandoffContractError("new transitions must have from_observation_id=null")
        object.__setattr__(self, "evidences", _evidence_tuple(self.evidences))
        object.__setattr__(self, "confidence", _confidence(self.confidence))
        object.__setattr__(self, "review_status", _enum(self.review_status, "review_status", REVIEW_STATUSES))

    def to_dict(self) -> dict[str, Any]:
        return {
            "transition_id": self.transition_id, "claim_key": self.claim_key, "scope_key": self.scope_key,
            "to_observation_id": self.to_observation_id, "transition_kind": self.transition_kind,
            "reason": self.reason, "evidences": [item.to_dict() for item in self.evidences],
            "from_observation_id": self.from_observation_id, "confidence": self.confidence,
            "review_status": self.review_status,
        }

    @property
    def evidence_ids(self) -> tuple[str, ...]:
        return tuple(item.evidence_id for item in self.evidences)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "StateTransition":
        data = _as_mapping(value, cls.__name__)
        _check_fields(data, record=cls.__name__, allowed=cls._FIELDS, required=cls._REQUIRED)
        return cls(
            transition_id=data["transition_id"], claim_key=data["claim_key"], scope_key=data["scope_key"],
            to_observation_id=data["to_observation_id"], transition_kind=data["transition_kind"],
            reason=data["reason"], evidences=_evidence_tuple(data["evidences"]),
            from_observation_id=data.get("from_observation_id"), confidence=data.get("confidence", 0.5),
            review_status=data.get("review_status", "pending_review"),
        )


@dataclass(frozen=True)
class MemoryWriteHandoffItem:
    idempotency_key: str
    operation_id: str
    workspace_id: str
    project_id: str
    domain_profile: str
    profile_version: str
    memory_type: str
    content: str
    source_id: str
    source_unit_id: str
    source_scope: HandoffSourceScope
    claim_key: str
    scope_key: str
    observed_at: str
    transition_kind: str
    evidences: tuple[HandoffEvidence, ...]
    task_id: str | None = None
    conversation_id: str | None = None
    subtype: str | None = None
    subject: str | None = None
    entity_type: str | None = None
    entity_id: str | None = None
    importance: str = "medium"
    declared_sensitivity: str = "private"
    requested_visibility: str = "private"
    assertion_source: str = "user_report"
    verification_state: str = "unverified"
    effective_from: str | None = None
    effective_to: str | None = None
    confidence: float = 0.5
    supersedes_ref: str | None = None
    supersedes_id: str | None = None
    expected_version: int | None = None
    domain_payload: dict[str, Any] = field(default_factory=dict)

    _FIELDS = frozenset({
        "idempotency_key", "operation_id", "workspace_id", "project_id", "task_id", "conversation_id",
        "domain_profile", "profile_version", "memory_type", "subtype", "subject", "entity_type", "entity_id",
        "content", "importance", "declared_sensitivity", "requested_visibility", "assertion_source",
        "verification_state", "effective_from", "effective_to", "source_id", "source_unit_id", "source_scope",
        "confidence", "supersedes_ref", "supersedes_id", "expected_version", "domain_payload", "evidences",
        "claim_key", "scope_key", "observed_at", "transition_kind",
    })
    _REQUIRED = (
        "idempotency_key", "operation_id", "workspace_id", "project_id", "domain_profile", "profile_version",
        "memory_type", "content", "source_id", "source_unit_id", "source_scope", "claim_key", "scope_key",
        "observed_at", "transition_kind", "evidences",
    )

    def __post_init__(self) -> None:
        for field_name in (
            "idempotency_key", "operation_id", "workspace_id", "project_id", "domain_profile",
            "profile_version", "content", "source_id", "source_unit_id", "claim_key", "scope_key",
        ):
            object.__setattr__(self, field_name, _text(getattr(self, field_name), field_name))
        for field_name in (
            "task_id", "conversation_id", "subtype", "subject", "entity_type", "entity_id",
            "supersedes_ref", "supersedes_id",
        ):
            object.__setattr__(self, field_name, _optional_text(getattr(self, field_name), field_name))
        object.__setattr__(self, "memory_type", _enum(self.memory_type, "memory_type", MEMORY_TYPES))
        object.__setattr__(self, "importance", _enum(self.importance, "importance", IMPORTANCE_LEVELS))
        object.__setattr__(self, "declared_sensitivity", _enum(self.declared_sensitivity, "declared_sensitivity", SENSITIVITY_LEVELS))
        object.__setattr__(self, "requested_visibility", _enum(self.requested_visibility, "requested_visibility", VISIBILITY_LEVELS))
        object.__setattr__(self, "assertion_source", _enum(self.assertion_source, "assertion_source", ASSERTION_SOURCES))
        object.__setattr__(self, "verification_state", _enum(self.verification_state, "verification_state", VERIFICATION_STATES))
        object.__setattr__(self, "source_scope", _coerce_scope(self.source_scope))
        object.__setattr__(self, "observed_at", _datetime_text(self.observed_at, "observed_at"))
        object.__setattr__(self, "transition_kind", _enum(self.transition_kind, "transition_kind", TRANSITION_KINDS))
        object.__setattr__(self, "evidences", _evidence_tuple(self.evidences))
        object.__setattr__(self, "effective_from", _datetime_text(self.effective_from, "effective_from", optional=True))
        object.__setattr__(self, "effective_to", _datetime_text(self.effective_to, "effective_to", optional=True))
        _validate_effective_range(self.effective_from, self.effective_to)
        object.__setattr__(self, "confidence", _confidence(self.confidence))
        if self.expected_version is not None:
            object.__setattr__(self, "expected_version", _integer(self.expected_version, "expected_version", minimum=0))
        object.__setattr__(self, "domain_payload", _json_object(self.domain_payload, "domain_payload"))
        if self.transition_kind == "supersedes" and not (self.supersedes_ref or self.supersedes_id):
            raise HandoffContractError("supersedes transitions require supersedes_ref or supersedes_id")

    def to_dict(self) -> dict[str, Any]:
        return {
            "idempotency_key": self.idempotency_key, "operation_id": self.operation_id,
            "workspace_id": self.workspace_id, "project_id": self.project_id, "task_id": self.task_id,
            "conversation_id": self.conversation_id, "domain_profile": self.domain_profile,
            "profile_version": self.profile_version, "memory_type": self.memory_type,
            "subtype": self.subtype, "subject": self.subject, "entity_type": self.entity_type,
            "entity_id": self.entity_id, "content": self.content, "importance": self.importance,
            "declared_sensitivity": self.declared_sensitivity,
            "requested_visibility": self.requested_visibility,
            "assertion_source": self.assertion_source, "verification_state": self.verification_state,
            "effective_from": self.effective_from, "effective_to": self.effective_to,
            "source_id": self.source_id, "source_unit_id": self.source_unit_id,
            "source_scope": self.source_scope.to_dict(), "confidence": self.confidence,
            "supersedes_ref": self.supersedes_ref, "supersedes_id": self.supersedes_id,
            "expected_version": self.expected_version,
            "domain_payload": _json_value(self.domain_payload, "domain_payload"),
            "evidences": [item.to_dict() for item in self.evidences],
            "claim_key": self.claim_key, "scope_key": self.scope_key,
            "observed_at": self.observed_at, "transition_kind": self.transition_kind,
        }

    @property
    def evidence_ids(self) -> tuple[str, ...]:
        return tuple(item.evidence_id for item in self.evidences)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "MemoryWriteHandoffItem":
        data = _as_mapping(value, cls.__name__)
        _check_fields(data, record=cls.__name__, allowed=cls._FIELDS, required=cls._REQUIRED)
        return cls(
            idempotency_key=data["idempotency_key"], operation_id=data["operation_id"],
            workspace_id=data["workspace_id"], project_id=data["project_id"],
            domain_profile=data["domain_profile"], profile_version=data["profile_version"],
            memory_type=data["memory_type"], content=data["content"], source_id=data["source_id"],
            source_unit_id=data["source_unit_id"], source_scope=_coerce_scope(data["source_scope"]),
            claim_key=data["claim_key"], scope_key=data["scope_key"], observed_at=data["observed_at"],
            transition_kind=data["transition_kind"], evidences=_evidence_tuple(data["evidences"]),
            task_id=data.get("task_id"), conversation_id=data.get("conversation_id"),
            subtype=data.get("subtype"), subject=data.get("subject"),
            entity_type=data.get("entity_type"), entity_id=data.get("entity_id"),
            importance=data.get("importance", "medium"),
            declared_sensitivity=data.get("declared_sensitivity", "private"),
            requested_visibility=data.get("requested_visibility", "private"),
            assertion_source=data.get("assertion_source", "user_report"),
            verification_state=data.get("verification_state", "unverified"),
            effective_from=data.get("effective_from"), effective_to=data.get("effective_to"),
            confidence=data.get("confidence", 0.5), supersedes_ref=data.get("supersedes_ref"),
            supersedes_id=data.get("supersedes_id"), expected_version=data.get("expected_version"),
            domain_payload=data.get("domain_payload", {}),
        )


@dataclass(frozen=True)
class PendingReviewItem:
    review_item_id: str
    claim_key: str
    scope_key: str
    reason: str
    transition_kind: str
    evidences: tuple[HandoffEvidence, ...]
    observation_id: str | None = None
    conversation_id: str | None = None
    content: str | None = None
    related_observation_ids: tuple[str, ...] = ()
    review_status: str = "pending_review"
    suggested_action: str = "manual_review_required"

    _FIELDS = frozenset({
        "review_item_id", "claim_key", "scope_key", "reason", "transition_kind", "evidences",
        "observation_id", "conversation_id", "content", "related_observation_ids", "review_status",
        "suggested_action",
    })
    _REQUIRED = ("review_item_id", "claim_key", "scope_key", "reason", "transition_kind", "evidences")

    def __post_init__(self) -> None:
        for field_name in ("review_item_id", "claim_key", "scope_key", "reason", "suggested_action"):
            object.__setattr__(self, field_name, _text(getattr(self, field_name), field_name))
        object.__setattr__(self, "transition_kind", _enum(self.transition_kind, "transition_kind", PENDING_TRANSITION_KINDS))
        object.__setattr__(self, "evidences", _evidence_tuple(self.evidences))
        object.__setattr__(self, "observation_id", _optional_text(self.observation_id, "observation_id"))
        object.__setattr__(self, "conversation_id", _optional_text(self.conversation_id, "conversation_id"))
        object.__setattr__(self, "content", _optional_text(self.content, "content"))
        object.__setattr__(self, "related_observation_ids", _unique_text_sequence(self.related_observation_ids, "related_observation_ids"))
        if not self.observation_id and not self.related_observation_ids:
            raise HandoffContractError("pending review items must reference at least one observation")
        object.__setattr__(self, "review_status", _enum(self.review_status, "review_status", ("pending_review", "needs_review")))

    def to_dict(self) -> dict[str, Any]:
        return {
            "review_item_id": self.review_item_id, "claim_key": self.claim_key,
            "scope_key": self.scope_key, "reason": self.reason,
            "transition_kind": self.transition_kind,
            "evidences": [item.to_dict() for item in self.evidences],
            "observation_id": self.observation_id, "conversation_id": self.conversation_id,
            "content": self.content, "related_observation_ids": list(self.related_observation_ids),
            "review_status": self.review_status, "suggested_action": self.suggested_action,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PendingReviewItem":
        data = _as_mapping(value, cls.__name__)
        _check_fields(data, record=cls.__name__, allowed=cls._FIELDS, required=cls._REQUIRED)
        return cls(
            review_item_id=data["review_item_id"], claim_key=data["claim_key"], scope_key=data["scope_key"],
            reason=data["reason"], transition_kind=data["transition_kind"],
            evidences=_evidence_tuple(data["evidences"]), observation_id=data.get("observation_id"),
            conversation_id=data.get("conversation_id"), content=data.get("content"),
            related_observation_ids=data.get("related_observation_ids", ()),
            review_status=data.get("review_status", "pending_review"),
            suggested_action=data.get("suggested_action", "manual_review_required"),
        )


def _string_map(value: Any, field_name: str) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise HandoffContractError(f"{field_name} must be an object")
    result: dict[str, str] = {}
    for key, item in value.items():
        normalized_key = _text(key, f"{field_name} key")
        normalized_value = _text(item, f"{field_name}.{key}")
        assert normalized_key is not None and normalized_value is not None
        result[normalized_key] = normalized_value
    return result


def _count_map(value: Any, field_name: str) -> dict[str, int]:
    if not isinstance(value, Mapping):
        raise HandoffContractError(f"{field_name} must be an object")
    result: dict[str, int] = {}
    for key, item in value.items():
        normalized_key = _text(key, f"{field_name} key")
        assert normalized_key is not None
        result[normalized_key] = _integer(item, f"{field_name}.{key}", minimum=0)
    return result


@dataclass(frozen=True)
class HandoffManifest:
    handoff_id: str
    created_at: str
    plan_id: str
    run_id: str
    source_sha256: str
    workspace_id: str
    project_id: str
    conversation_ids: tuple[str, ...]
    domain_profile: str
    profile_version: str
    artifact_hashes: dict[str, str]
    counts: dict[str, int]
    contract_version: str = HANDOFF_CONTRACT_VERSION
    production_writes: bool = False
    canonical_writes: bool = False
    milvus_writes: bool = False
    alias_changes: bool = False
    contains_security_fields: bool = False

    _FIELDS = frozenset({
        "handoff_id", "created_at", "plan_id", "run_id", "source_sha256", "workspace_id", "project_id",
        "conversation_ids", "domain_profile", "profile_version", "artifact_hashes", "counts",
        "contract_version", "production_writes", "canonical_writes", "milvus_writes", "alias_changes",
        "contains_security_fields",
    })
    _REQUIRED = (
        "handoff_id", "created_at", "plan_id", "run_id", "source_sha256", "workspace_id", "project_id",
        "conversation_ids", "domain_profile", "profile_version", "artifact_hashes", "counts",
    )

    def __post_init__(self) -> None:
        for field_name in (
            "handoff_id", "plan_id", "run_id", "source_sha256", "workspace_id", "project_id",
            "domain_profile", "profile_version",
        ):
            object.__setattr__(self, field_name, _text(getattr(self, field_name), field_name))
        object.__setattr__(self, "created_at", _datetime_text(self.created_at, "created_at"))
        object.__setattr__(self, "conversation_ids", _unique_text_sequence(self.conversation_ids, "conversation_ids", nonempty=True))
        if self.contract_version != HANDOFF_CONTRACT_VERSION:
            raise HandoffContractError(f"contract_version must be {HANDOFF_CONTRACT_VERSION}")
        for field_name in (
            "production_writes", "canonical_writes", "milvus_writes", "alias_changes", "contains_security_fields",
        ):
            value = _boolean(getattr(self, field_name), field_name)
            if value:
                raise HandoffContractError(f"{field_name} must be false")
            object.__setattr__(self, field_name, value)
        object.__setattr__(self, "artifact_hashes", _string_map(self.artifact_hashes, "artifact_hashes"))
        object.__setattr__(self, "counts", _count_map(self.counts, "counts"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "handoff_id": self.handoff_id, "created_at": self.created_at, "plan_id": self.plan_id,
            "run_id": self.run_id, "source_sha256": self.source_sha256,
            "workspace_id": self.workspace_id, "project_id": self.project_id,
            "conversation_ids": list(self.conversation_ids), "domain_profile": self.domain_profile,
            "profile_version": self.profile_version, "artifact_hashes": dict(self.artifact_hashes),
            "counts": dict(self.counts), "contract_version": self.contract_version,
            "production_writes": self.production_writes, "canonical_writes": self.canonical_writes,
            "milvus_writes": self.milvus_writes, "alias_changes": self.alias_changes,
            "contains_security_fields": self.contains_security_fields,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "HandoffManifest":
        data = _as_mapping(value, cls.__name__)
        _check_fields(data, record=cls.__name__, allowed=cls._FIELDS, required=cls._REQUIRED)
        return cls(
            handoff_id=data["handoff_id"], created_at=data["created_at"], plan_id=data["plan_id"],
            run_id=data["run_id"], source_sha256=data["source_sha256"], workspace_id=data["workspace_id"],
            project_id=data["project_id"], conversation_ids=data["conversation_ids"],
            domain_profile=data["domain_profile"], profile_version=data["profile_version"],
            artifact_hashes=data["artifact_hashes"], counts=data["counts"],
            contract_version=data.get("contract_version", HANDOFF_CONTRACT_VERSION),
            production_writes=data.get("production_writes", False),
            canonical_writes=data.get("canonical_writes", False),
            milvus_writes=data.get("milvus_writes", False),
            alias_changes=data.get("alias_changes", False),
            contains_security_fields=data.get("contains_security_fields", False),
        )


def _canonical_evidence(evidence: HandoffEvidence) -> dict[str, Any]:
    return evidence.to_dict()


def _idempotency_material(item: MemoryWriteHandoffItem) -> dict[str, Any]:
    values = item.to_dict()
    values.pop("idempotency_key")
    values["observed_at"] = _canonical_datetime(values["observed_at"])
    values["effective_from"] = _canonical_datetime(values["effective_from"])
    values["effective_to"] = _canonical_datetime(values["effective_to"])
    values["evidences"] = sorted(
        (_canonical_evidence(evidence) for evidence in item.evidences),
        key=lambda evidence: json.dumps(
            evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
        ),
    )
    return values


def generate_idempotency_key(
    item_or_values: MemoryWriteHandoffItem | Mapping[str, Any] | None = None,
    **field_values: Any,
) -> str:
    """Return a deterministic v1 key from the complete handoff item payload."""

    if item_or_values is None:
        if not field_values:
            raise HandoffContractError("idempotency key generation requires item fields")
        values = dict(field_values)
        values.setdefault("idempotency_key", "mwh_pending")
        item = MemoryWriteHandoffItem.from_dict(values)
    elif isinstance(item_or_values, MemoryWriteHandoffItem):
        if field_values:
            raise HandoffContractError("do not mix a handoff item with field overrides")
        item = item_or_values
    elif isinstance(item_or_values, Mapping):
        values = dict(item_or_values)
        values.update(field_values)
        values.setdefault("idempotency_key", "mwh_pending")
        item = MemoryWriteHandoffItem.from_dict(values)
    else:
        raise HandoffContractError("generate_idempotency_key expects MemoryWriteHandoffItem or a mapping")
    payload = {"contract_version": HANDOFF_CONTRACT_VERSION, "item": _idempotency_material(item)}
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return "mwh_" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def materialize_idempotency_key(values: Mapping[str, Any]) -> MemoryWriteHandoffItem:
    """Validate a mapping and return the item with its generated idempotency key."""

    data = dict(values)
    data["idempotency_key"] = generate_idempotency_key(data)
    return MemoryWriteHandoffItem.from_dict(data)
