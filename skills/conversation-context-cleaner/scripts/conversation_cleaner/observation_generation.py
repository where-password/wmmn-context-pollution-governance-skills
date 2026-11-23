from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .contracts import Candidate, Message
from .handoff_contracts import EVIDENCE_STATUSES, STATE_KINDS, MemoryObservation
from .state_resolution import StateResolutionError, build_scope_key, candidate_to_observation


OBSERVATION_METADATA_FIELDS = frozenset({"claim_key", "scope_key", "state_kind", "observed_at", "effective_from", "effective_to"})


class ObservationGenerationError(ValueError):
    """Raised when controlled candidates/messages cannot produce strict observations."""


@dataclass(frozen=True)
class ObservationGenerationIssue:
    issue_id: str
    issue_code: str
    conversation_id: str
    message_id: str
    source_sequence: int
    candidate_ids: tuple[str, ...]
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "issue_id": self.issue_id,
            "issue_code": self.issue_code,
            "conversation_id": self.conversation_id,
            "message_id": self.message_id,
            "source_sequence": self.source_sequence,
            "candidate_ids": list(self.candidate_ids),
            "detail": self.detail,
        }


@dataclass(frozen=True)
class ObservationGenerationResult:
    observations: tuple[MemoryObservation, ...]
    issues: tuple[ObservationGenerationIssue, ...]

    @property
    def counts(self) -> dict[str, int]:
        return {"observations": len(self.observations), "issues": len(self.issues)}

    def to_dict(self) -> dict[str, Any]:
        return {
            "observations": [item.to_dict() for item in self.observations],
            "issues": [item.to_dict() for item in self.issues],
            "counts": self.counts,
        }


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash_id(prefix: str, value: Any) -> str:
    return prefix + hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()[:24]


def _required_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ObservationGenerationError(f"{field_name} must be a non-empty string")
    return value


def _optional_text(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, field_name)


def _datetime_text(value: Any, field_name: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    text = _required_text(value, field_name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ObservationGenerationError(f"{field_name} must be RFC3339 date-time") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ObservationGenerationError(f"{field_name} must include a timezone offset")
    return text


def _validate_effective_range(effective_from: str | None, effective_to: str | None) -> None:
    if effective_from is None or effective_to is None:
        return
    start = datetime.fromisoformat(effective_from.replace("Z", "+00:00"))
    end = datetime.fromisoformat(effective_to.replace("Z", "+00:00"))
    if end < start:
        raise ObservationGenerationError("effective_to must not precede effective_from")


def _observation_metadata(message: Message) -> dict[str, str | None] | None:
    if not isinstance(message.metadata, Mapping):
        raise ObservationGenerationError("message metadata must be an object")
    if "observation" not in message.metadata:
        return None
    raw = message.metadata["observation"]
    if not isinstance(raw, Mapping):
        raise ObservationGenerationError("metadata.observation must be an object")
    unknown = set(raw) - OBSERVATION_METADATA_FIELDS
    if unknown:
        raise ObservationGenerationError(f"metadata.observation contains unknown fields: {', '.join(sorted(unknown))}")
    if "claim_key" not in raw:
        raise ObservationGenerationError("metadata.observation.claim_key is required")
    claim_key = _required_text(raw.get("claim_key"), "metadata.observation.claim_key")
    scope_key = _optional_text(raw.get("scope_key"), "metadata.observation.scope_key")
    state_kind = _optional_text(raw.get("state_kind"), "metadata.observation.state_kind")
    if state_kind is not None and state_kind not in STATE_KINDS:
        raise ObservationGenerationError("metadata.observation.state_kind is invalid")
    observed_at = _datetime_text(raw.get("observed_at"), "metadata.observation.observed_at", optional=True)
    effective_from = _datetime_text(raw.get("effective_from"), "metadata.observation.effective_from", optional=True)
    effective_to = _datetime_text(raw.get("effective_to"), "metadata.observation.effective_to", optional=True)
    _validate_effective_range(effective_from, effective_to)
    return {
        "claim_key": claim_key,
        "scope_key": scope_key,
        "state_kind": state_kind,
        "observed_at": observed_at,
        "effective_from": effective_from,
        "effective_to": effective_to,
    }


def _message_index(messages: tuple[Message, ...]) -> tuple[dict[tuple[str, str], Message], dict[str, Message]]:
    by_message: dict[tuple[str, str], Message] = {}
    by_source_unit: dict[str, Message] = {}
    for message in messages:
        key = (message.conversation_id, message.message_id)
        if key in by_message:
            raise ObservationGenerationError("messages contain duplicate conversation/message_id pairs")
        if message.source_unit_id in by_source_unit:
            raise ObservationGenerationError("messages contain duplicate source_unit_id values")
        if not isinstance(message.sequence, int) or isinstance(message.sequence, bool) or message.sequence < 0:
            raise ObservationGenerationError("message sequence must be a non-negative integer")
        by_message[key] = message
        by_source_unit[message.source_unit_id] = message
    return by_message, by_source_unit


def _candidate_message(
    candidate: Candidate,
    messages_by_key: dict[tuple[str, str], Message],
    messages_by_source_unit: dict[str, Message],
) -> Message:
    if not candidate.evidence:
        raise ObservationGenerationError(f"candidate {candidate.candidate_id} has no evidence")
    selected: Message | None = None
    primary_message_id = getattr(candidate, "primary_evidence_message_id", None)
    for evidence in candidate.evidence:
        message = messages_by_key.get((candidate.conversation_id, evidence.message_id))
        if message is None:
            raise ObservationGenerationError(f"candidate {candidate.candidate_id} references a missing message")
        if messages_by_source_unit.get(evidence.source_unit_id) is not message:
            raise ObservationGenerationError(f"candidate {candidate.candidate_id} source_unit_id/message_id conflict")
        if message.source_unit_id != evidence.source_unit_id:
            raise ObservationGenerationError(f"candidate {candidate.candidate_id} evidence source unit conflicts with message")
        if evidence.role != message.role:
            raise ObservationGenerationError(f"candidate {candidate.candidate_id} evidence role conflicts with message")
        if evidence.source_unit_hash != message.source_unit_hash:
            raise ObservationGenerationError(f"candidate {candidate.candidate_id} source unit hash conflicts with message")
        if evidence.status not in EVIDENCE_STATUSES:
            raise ObservationGenerationError(f"candidate {candidate.candidate_id} has an invalid evidence status")
        if (
            isinstance(evidence.char_start, bool)
            or isinstance(evidence.char_end, bool)
            or not isinstance(evidence.char_start, int)
            or not isinstance(evidence.char_end, int)
            or evidence.char_start < 0
            or evidence.char_end < evidence.char_start
            or evidence.char_end > len(message.text)
            or message.text[evidence.char_start:evidence.char_end] != evidence.quote
        ):
            raise ObservationGenerationError(f"candidate {candidate.candidate_id} evidence span does not match the message")
        if primary_message_id is None:
            if selected is not None and selected is not message:
                raise ObservationGenerationError(f"candidate {candidate.candidate_id} spans multiple messages")
            selected = message
        elif evidence.message_id == primary_message_id:
            selected = message
    if selected is None:
        raise ObservationGenerationError(f"candidate {candidate.candidate_id} has no message mapping")
    return selected


def _observation_id(
    *,
    source_id: str,
    domain_profile: str,
    profile_version: str,
    candidate: Candidate,
    message: Message,
    claim_key: str,
    scope_key: str,
    observed_at: str,
) -> str:
    return _hash_id(
        "obs_",
        {
            "source_id": source_id,
            "domain_profile": domain_profile,
            "profile_version": profile_version,
            "conversation_id": message.conversation_id,
            "message_id": message.message_id,
            "source_unit_id": message.source_unit_id,
            "candidate_id": candidate.candidate_id,
            "claim_key": claim_key,
            "scope_key": scope_key,
            "observed_at": observed_at,
            "content": candidate.content,
        },
    )


def generate_observations(
    candidates: Iterable[Candidate],
    messages: Iterable[Message],
    *,
    workspace_id: str,
    project_id: str,
    source_id: str,
    domain_profile: str,
    profile_version: str,
    subject: str | None = None,
    entity_type: str | None = None,
    entity_id: str | None = None,
) -> ObservationGenerationResult:
    """Generate strict observations from candidates and their original messages."""

    for field_name, value in (
        ("workspace_id", workspace_id),
        ("project_id", project_id),
        ("source_id", source_id),
        ("domain_profile", domain_profile),
        ("profile_version", profile_version),
    ):
        _required_text(value, field_name)

    candidate_list = tuple(candidates)
    message_list = tuple(messages)
    messages_by_key, messages_by_source_unit = _message_index(message_list)
    metadata_by_message: dict[tuple[str, str], dict[str, str | None] | None] = {}
    for message in message_list:
        metadata_by_message[(message.conversation_id, message.message_id)] = _observation_metadata(message)

    candidate_ids: set[str] = set()
    by_message: dict[tuple[str, str], list[Candidate]] = {}
    for candidate in candidate_list:
        if candidate.candidate_id in candidate_ids:
            raise ObservationGenerationError(f"duplicate candidate_id: {candidate.candidate_id}")
        candidate_ids.add(candidate.candidate_id)
        if candidate.workspace_id != workspace_id or candidate.project_id != project_id:
            raise ObservationGenerationError(f"candidate {candidate.candidate_id} workspace/project mismatch")
        if candidate.domain_profile != domain_profile or candidate.profile_version != profile_version:
            raise ObservationGenerationError(f"candidate {candidate.candidate_id} domain/profile mismatch")
        message = _candidate_message(candidate, messages_by_key, messages_by_source_unit)
        by_message.setdefault((message.conversation_id, message.message_id), []).append(candidate)

    fallback_scope = build_scope_key(
        workspace_id=workspace_id,
        project_id=project_id,
        subject=subject,
        entity_type=entity_type,
        entity_id=entity_id,
    )
    observations: list[MemoryObservation] = []
    issues: list[ObservationGenerationIssue] = []

    for message_key in sorted(by_message, key=lambda key: (key[0], messages_by_key[key].sequence, key[1])):
        message = messages_by_key[message_key]
        group = sorted(by_message[message_key], key=lambda item: item.candidate_id)
        metadata = metadata_by_message[message_key]
        if metadata is not None and len(group) != 1:
            candidate_group_ids = tuple(item.candidate_id for item in group)
            detail = "Explicit observation metadata on a message with multiple candidates cannot be assigned safely."
            issues.append(
                ObservationGenerationIssue(
                    issue_id=_hash_id(
                        "issue_",
                        {
                            "issue_code": "ambiguous_observation_metadata",
                            "conversation_id": message.conversation_id,
                            "message_id": message.message_id,
                            "candidate_ids": list(candidate_group_ids),
                        },
                    ),
                    issue_code="ambiguous_observation_metadata",
                    conversation_id=message.conversation_id,
                    message_id=message.message_id,
                    source_sequence=message.sequence,
                    candidate_ids=candidate_group_ids,
                    detail=detail,
                )
            )
            continue

        for candidate in group:
            candidate_claim_key = getattr(candidate, "claim_key", None)
            candidate_scope_key = getattr(candidate, "scope_key", None)
            candidate_observed_at = getattr(candidate, "observed_at", None)
            candidate_state_kind = getattr(candidate, "state_kind", None)
            candidate_effective_from = getattr(candidate, "effective_from", None)
            candidate_effective_to = getattr(candidate, "effective_to", None)
            claim_key = (
                metadata["claim_key"] if metadata is not None
                else candidate_claim_key or f"candidate:{candidate.candidate_id}"
            )
            assert claim_key is not None
            scope_key = (
                (metadata.get("scope_key") if metadata is not None else None)
                or candidate_scope_key
                or fallback_scope
            )
            observed_at = (
                (metadata.get("observed_at") if metadata is not None else None)
                or candidate_observed_at
                or message.timestamp
            )
            state_kind = (
                metadata.get("state_kind") if metadata is not None else candidate_state_kind
            )
            effective_from = (
                metadata.get("effective_from") if metadata is not None else candidate_effective_from
            )
            effective_to = (
                metadata.get("effective_to") if metadata is not None else candidate_effective_to
            )
            observation_id = _observation_id(
                source_id=source_id,
                domain_profile=domain_profile,
                profile_version=profile_version,
                candidate=candidate,
                message=message,
                claim_key=claim_key,
                scope_key=scope_key,
                observed_at=observed_at,
            )
            try:
                observation = candidate_to_observation(
                    candidate,
                    observed_at=observed_at,
                    source_sequence=message.sequence,
                    scope_key=scope_key,
                    claim_key=claim_key,
                    state_kind=state_kind,
                    effective_from=effective_from,
                    effective_to=effective_to,
                    observation_id=observation_id,
                )
            except (StateResolutionError, ValueError) as exc:
                raise ObservationGenerationError(f"candidate {candidate.candidate_id} could not produce an observation") from exc
            observations.append(observation)

    observations.sort(key=lambda item: (item.conversation_id, item.source_sequence, item.observation_id))
    issues.sort(key=lambda item: (item.conversation_id, item.source_sequence, item.message_id, item.issue_id))
    return ObservationGenerationResult(observations=tuple(observations), issues=tuple(issues))


generate_observation_result = generate_observations
