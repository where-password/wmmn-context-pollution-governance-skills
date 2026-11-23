from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from .handoff_approval import (
    HandoffApproval,
    HandoffApprovalError,
    require_handoff_approval,
)
from .handoff_contracts import HANDOFF_CONTRACT_VERSION, MemoryWriteHandoffItem


MEMORY_WRITE_INPUT_FIELDS = frozenset(
    {
        "idempotency_key", "operation_id", "workspace_id", "project_id", "task_id",
        "conversation_id", "domain_profile", "profile_version", "memory_type", "subtype",
        "subject", "entity_type", "entity_id", "content", "importance", "declared_sensitivity",
        "requested_visibility", "assertion_source", "verification_state", "effective_from",
        "effective_to", "source_id", "source_unit_id", "source_scope", "confidence",
        "supersedes_id", "expected_version", "domain_payload", "evidences",
    }
)
HANDOFF_ONLY_FIELDS = frozenset({"claim_key", "scope_key", "observed_at", "transition_kind", "supersedes_ref"})


class DatabaseHandoffAdapterError(ValueError):
    """Raised when handoff data cannot be converted to a database write input."""


def _coerce_item(value: MemoryWriteHandoffItem | Mapping[str, Any]) -> MemoryWriteHandoffItem:
    if isinstance(value, MemoryWriteHandoffItem):
        return value
    if isinstance(value, Mapping):
        try:
            return MemoryWriteHandoffItem.from_dict(value)
        except ValueError as exc:
            raise DatabaseHandoffAdapterError("handoff item failed strict contract validation") from exc
    raise DatabaseHandoffAdapterError("handoff item must be MemoryWriteHandoffItem or a mapping")


def resolve_supersedes_id(
    item: MemoryWriteHandoffItem | Mapping[str, Any],
    supersedes_ref_to_id: Mapping[str, str] | None,
) -> str | None:
    """Resolve a logical supersedes reference using only a caller-supplied map."""

    handoff = _coerce_item(item)
    ref = handoff.supersedes_ref
    existing = handoff.supersedes_id
    if ref is None:
        return existing
    if supersedes_ref_to_id is None:
        raise DatabaseHandoffAdapterError("supersedes_ref requires an explicit supersedes_ref_to_id map")
    try:
        resolved = supersedes_ref_to_id[ref]
    except KeyError as exc:
        raise DatabaseHandoffAdapterError(f"supersedes_ref has no explicit memory_id mapping: {ref}") from exc
    if not isinstance(resolved, str) or not resolved.strip():
        raise DatabaseHandoffAdapterError("resolved supersedes_id must be a non-empty string")
    if existing is not None and existing != resolved:
        raise DatabaseHandoffAdapterError("handoff supersedes_id conflicts with the explicit mapping")
    return resolved


def _resolve_expected_version(handoff: MemoryWriteHandoffItem, expected_version: int | None) -> int | None:
    if expected_version is not None and (isinstance(expected_version, bool) or not isinstance(expected_version, int)):
        raise DatabaseHandoffAdapterError("expected_version must be an integer or null")
    if expected_version is not None and expected_version < 0:
        raise DatabaseHandoffAdapterError("expected_version must be non-negative")
    stored = handoff.expected_version
    if expected_version is not None and stored is not None and expected_version != stored:
        raise DatabaseHandoffAdapterError("explicit expected_version conflicts with the handoff item")
    return expected_version if expected_version is not None else stored


def _database_evidence_id(idempotency_key: str, handoff_evidence_id: str) -> str:
    material = json.dumps(
        {
            "contract_version": HANDOFF_CONTRACT_VERSION,
            "idempotency_key": idempotency_key,
            "handoff_evidence_id": handoff_evidence_id,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "ev_" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def _database_evidences(handoff: MemoryWriteHandoffItem) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for evidence in handoff.evidences:
        locator = dict(evidence.locator_json)
        existing = locator.get("handoff_evidence_id")
        if existing is not None and existing != evidence.evidence_id:
            raise DatabaseHandoffAdapterError("locator_json conflicts with the handoff evidence_id")
        locator["handoff_evidence_id"] = evidence.evidence_id
        result.append(
            {
                "evidence_id": _database_evidence_id(handoff.idempotency_key, evidence.evidence_id),
                "source_unit_id": evidence.source_unit_id,
                "quote": evidence.quote,
                "locator_json": locator,
                "status": evidence.status,
            }
        )
    return result


def to_memory_write_input(
    item: MemoryWriteHandoffItem | Mapping[str, Any],
    *,
    handoff_id: str,
    approval: HandoffApproval | Mapping[str, Any] | None = None,
    supersedes_ref_to_id: Mapping[str, str] | None = None,
    expected_version: int | None = None,
) -> dict[str, Any]:
    """Map an approved handoff item to the strict MemoryWriteInput JSON shape."""

    handoff = _coerce_item(item)
    try:
        approved = require_handoff_approval(approval, handoff, handoff_id=handoff_id)
    except HandoffApprovalError as exc:
        raise DatabaseHandoffAdapterError(f"handoff approval rejected: {exc}") from exc
    domain_payload = dict(handoff.domain_payload)
    if "conversation_handoff" in domain_payload:
        raise DatabaseHandoffAdapterError("domain_payload already contains reserved conversation_handoff metadata")
    domain_payload["conversation_handoff"] = {
        "contract_version": HANDOFF_CONTRACT_VERSION,
        "claim_key": handoff.claim_key,
        "scope_key": handoff.scope_key,
        "observed_at": handoff.observed_at,
        "transition_kind": handoff.transition_kind,
        "supersedes_ref": handoff.supersedes_ref,
        "approval_id": approved.approval_id,
    }
    result = {
        "idempotency_key": handoff.idempotency_key,
        "operation_id": handoff.operation_id,
        "workspace_id": handoff.workspace_id,
        "project_id": handoff.project_id,
        "task_id": handoff.task_id,
        "conversation_id": handoff.conversation_id,
        "domain_profile": handoff.domain_profile,
        "profile_version": handoff.profile_version,
        "memory_type": handoff.memory_type,
        "subtype": handoff.subtype,
        "subject": handoff.subject,
        "entity_type": handoff.entity_type,
        "entity_id": handoff.entity_id,
        "content": handoff.content,
        "importance": handoff.importance,
        "declared_sensitivity": handoff.declared_sensitivity,
        "requested_visibility": handoff.requested_visibility,
        "assertion_source": handoff.assertion_source,
        "verification_state": handoff.verification_state,
        "effective_from": handoff.effective_from,
        "effective_to": handoff.effective_to,
        "source_id": handoff.source_id,
        "source_unit_id": handoff.source_unit_id,
        "source_scope": handoff.source_scope.to_dict(),
        "confidence": handoff.confidence,
        "supersedes_id": resolve_supersedes_id(handoff, supersedes_ref_to_id),
        "expected_version": _resolve_expected_version(handoff, expected_version),
        "domain_payload": domain_payload,
        "evidences": _database_evidences(handoff),
    }
    if set(result) != MEMORY_WRITE_INPUT_FIELDS:
        raise DatabaseHandoffAdapterError("internal adapter field map does not match MemoryWriteInput")
    return result


adapt_handoff_item = to_memory_write_input
handoff_to_memory_write_input = to_memory_write_input
