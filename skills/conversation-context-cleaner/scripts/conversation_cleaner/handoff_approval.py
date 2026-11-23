from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .handoff_contracts import MemoryWriteHandoffItem


APPROVAL_CONTRACT_VERSION = "conversation-memory-handoff-approval-v1"
APPROVAL_DECISIONS = ("approve", "reject", "defer", "revoke")
_APPROVAL_ID_RE = re.compile(r"^hap_[0-9a-f]{32}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class HandoffApprovalError(ValueError):
    """Raised when a handoff approval is invalid or does not bind to an item."""


def _required_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HandoffApprovalError(f"{field_name} must be a non-empty string")
    return value


def _datetime_text(value: Any, field_name: str) -> str:
    text = _required_text(value, field_name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HandoffApprovalError(f"{field_name} must be RFC3339 date-time") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise HandoffApprovalError(f"{field_name} must include a timezone offset")
    return text


def _coerce_item(value: MemoryWriteHandoffItem | Mapping[str, Any]) -> MemoryWriteHandoffItem:
    if isinstance(value, MemoryWriteHandoffItem):
        return value
    if isinstance(value, Mapping):
        try:
            return MemoryWriteHandoffItem.from_dict(value)
        except ValueError as exc:
            raise HandoffApprovalError("handoff item failed strict contract validation") from exc
    raise HandoffApprovalError("handoff item must be MemoryWriteHandoffItem or a mapping")


def _coerce_approval(value: "HandoffApproval | Mapping[str, Any]") -> "HandoffApproval":
    if isinstance(value, HandoffApproval):
        return value
    if isinstance(value, Mapping):
        return HandoffApproval.from_dict(value)
    raise HandoffApprovalError("approval must be HandoffApproval or a mapping")


def handoff_item_sha256(item: MemoryWriteHandoffItem | Mapping[str, Any]) -> str:
    """Hash the complete canonical handoff item, including evidence and transition data."""

    handoff = _coerce_item(item)
    payload = json.dumps(
        handoff.to_dict(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _approval_material(
    *,
    handoff_id: str,
    item_idempotency_key: str,
    item_sha256: str,
    decision: str,
    reviewer_id: str,
    reason: str,
    reviewed_at: str,
) -> dict[str, str]:
    return {
        "approval_contract_version": APPROVAL_CONTRACT_VERSION,
        "handoff_id": handoff_id,
        "item_idempotency_key": item_idempotency_key,
        "item_sha256": item_sha256,
        "decision": decision,
        "reviewer_id": reviewer_id,
        "reason": reason,
        "reviewed_at": reviewed_at,
    }


def _approval_id(**material: str) -> str:
    encoded = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "hap_" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True)
class HandoffApproval:
    approval_id: str
    handoff_id: str
    item_idempotency_key: str
    item_sha256: str
    decision: str
    reviewer_id: str
    reason: str
    reviewed_at: str

    _FIELDS = frozenset({
        "approval_id",
        "approval_contract_version",
        "handoff_id",
        "item_idempotency_key",
        "item_sha256",
        "decision",
        "reviewer_id",
        "reason",
        "reviewed_at",
    })
    _REQUIRED = (
        "approval_id",
        "approval_contract_version",
        "handoff_id",
        "item_idempotency_key",
        "item_sha256",
        "decision",
        "reviewer_id",
        "reason",
        "reviewed_at",
    )

    def __post_init__(self) -> None:
        for field_name in (
            "approval_id",
            "handoff_id",
            "item_idempotency_key",
            "item_sha256",
            "decision",
            "reviewer_id",
            "reason",
        ):
            object.__setattr__(self, field_name, _required_text(getattr(self, field_name), field_name))
        if self.decision not in APPROVAL_DECISIONS:
            raise HandoffApprovalError(f"decision must be one of: {', '.join(APPROVAL_DECISIONS)}")
        if not _APPROVAL_ID_RE.fullmatch(self.approval_id):
            raise HandoffApprovalError("approval_id has an invalid format")
        if not _SHA256_RE.fullmatch(self.item_sha256):
            raise HandoffApprovalError("item_sha256 must be a lowercase SHA-256 hex digest")
        object.__setattr__(self, "reviewed_at", _datetime_text(self.reviewed_at, "reviewed_at"))
        expected = _approval_id(
            **_approval_material(
                handoff_id=self.handoff_id,
                item_idempotency_key=self.item_idempotency_key,
                item_sha256=self.item_sha256,
                decision=self.decision,
                reviewer_id=self.reviewer_id,
                reason=self.reason,
                reviewed_at=self.reviewed_at,
            )
        )
        if self.approval_id != expected:
            raise HandoffApprovalError("approval_id does not match the approval fields")

    def to_dict(self) -> dict[str, str]:
        return {
            "approval_contract_version": APPROVAL_CONTRACT_VERSION,
            "approval_id": self.approval_id,
            "handoff_id": self.handoff_id,
            "item_idempotency_key": self.item_idempotency_key,
            "item_sha256": self.item_sha256,
            "decision": self.decision,
            "reviewer_id": self.reviewer_id,
            "reason": self.reason,
            "reviewed_at": self.reviewed_at,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "HandoffApproval":
        if not isinstance(value, Mapping):
            raise HandoffApprovalError("HandoffApproval must be a mapping")
        data = dict(value)
        unknown = set(data) - cls._FIELDS
        if unknown:
            raise HandoffApprovalError(f"HandoffApproval contains unknown fields: {', '.join(sorted(unknown))}")
        missing = [name for name in cls._REQUIRED if name not in data]
        if missing:
            raise HandoffApprovalError(f"HandoffApproval is missing required fields: {', '.join(missing)}")
        if data["approval_contract_version"] != APPROVAL_CONTRACT_VERSION:
            raise HandoffApprovalError(
                f"approval_contract_version must be {APPROVAL_CONTRACT_VERSION}"
            )
        fields = [name for name in cls._REQUIRED if name != "approval_contract_version"]
        return cls(**{name: data[name] for name in fields})


def create_handoff_approval(
    *,
    item: MemoryWriteHandoffItem | Mapping[str, Any],
    handoff_id: str,
    decision: str,
    reviewer_id: str,
    reason: str,
    reviewed_at: str,
) -> HandoffApproval:
    """Create a deterministic human approval record without reading a clock or storage."""

    handoff = _coerce_item(item)
    material = _approval_material(
        handoff_id=_required_text(handoff_id, "handoff_id"),
        item_idempotency_key=handoff.idempotency_key,
        item_sha256=handoff_item_sha256(handoff),
        decision=_required_text(decision, "decision"),
        reviewer_id=_required_text(reviewer_id, "reviewer_id"),
        reason=_required_text(reason, "reason"),
        reviewed_at=_datetime_text(reviewed_at, "reviewed_at"),
    )
    fields = {key: value for key, value in material.items() if key != "approval_contract_version"}
    return HandoffApproval(approval_id=_approval_id(**material), **fields)


def require_handoff_approval(
    approval: HandoffApproval | Mapping[str, Any],
    item: MemoryWriteHandoffItem | Mapping[str, Any],
    *,
    handoff_id: str,
) -> HandoffApproval:
    """Return the approval only when it is approved and bound to the exact item."""

    approved = _coerce_approval(approval)
    handoff = _coerce_item(item)
    current_handoff_id = _required_text(handoff_id, "handoff_id")
    if approved.decision != "approve":
        raise HandoffApprovalError("only an approve decision can authorize a database handoff write")
    if approved.handoff_id != current_handoff_id:
        raise HandoffApprovalError("approval handoff_id does not match the current handoff")
    if approved.item_idempotency_key != handoff.idempotency_key:
        raise HandoffApprovalError("approval item_idempotency_key does not match the handoff item")
    if approved.item_sha256 != handoff_item_sha256(handoff):
        raise HandoffApprovalError("approval item_sha256 does not match the handoff item")
    return approved


def bind_approval_to_item(
    approval: HandoffApproval | Mapping[str, Any],
    item: MemoryWriteHandoffItem | Mapping[str, Any],
    *,
    handoff_id: str,
) -> MemoryWriteHandoffItem:
    """Require an approve decision bound to the exact handoff item and hash."""

    require_handoff_approval(approval, item, handoff_id=handoff_id)
    return _coerce_item(item)


verify_handoff_approval = bind_approval_to_item
