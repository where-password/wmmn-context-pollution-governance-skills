from __future__ import annotations

from pathlib import Path
from typing import Any

from .handoff_contracts import MemoryWriteHandoffItem
from .handoff_review import (
    HandoffReviewError,
    handoff_approval_status,
    read_handoff_items,
    record_handoff_decision,
)
from .util import utc_now


_RISK_KEYS = {
    "split_review",
    "mixed_signal_claim",
    "needs_review",
    "conflict",
    "conflicts_with",
    "supersedes",
    "review_required",
    "auto_approve_blocked",
    "assistant_inference",
}
_ALLOWED_TRANSITIONS = {"new", "unchanged"}
_ALLOWED_ASSERTIONS = {"user_decision", "user_preference", "user_request"}


def _truthy_risk(value: Any) -> bool:
    return value not in (None, False, "", [], {})


def _risk_reason(item: MemoryWriteHandoffItem) -> str | None:
    if item.transition_kind not in _ALLOWED_TRANSITIONS:
        return "transition_requires_review"
    if item.assertion_source not in _ALLOWED_ASSERTIONS:
        return "assertion_source_not_user_decision"
    if item.verification_state != "quote_verified":
        return "verification_state_not_quote_verified"
    if item.supersedes_ref or item.supersedes_id or item.expected_version is not None:
        return "supersede_requires_review"
    if item.declared_sensitivity == "secret":
        return "secret_requires_review"
    if not item.evidences or not any(evidence.status == "verified" for evidence in item.evidences):
        return "verified_evidence_missing"
    for key in _RISK_KEYS:
        if key in item.domain_payload and _truthy_risk(item.domain_payload[key]):
            return f"risk_marker:{key}"
    return None


def auto_approve_handoff(
    *,
    handoff_dir: str | Path,
    handoff_id: str,
    reviewer_id: str = "local-auto-approval",
    reason: str = "auto-approved: quote-verified user decision without supersede/conflict",
    reviewed_at: str | None = None,
) -> dict[str, Any]:
    """Approve only the explicit low-risk subset; all other items remain human-reviewable."""

    if not isinstance(reviewer_id, str) or not reviewer_id.strip():
        raise HandoffReviewError("reviewer_id must be non-empty")
    if not isinstance(reason, str) or not reason.strip():
        raise HandoffReviewError("reason must be non-empty")
    items = read_handoff_items(handoff_dir)
    status = handoff_approval_status(handoff_dir=handoff_dir, handoff_id=handoff_id)
    latest = {
        item["idempotency_key"]: item["latest_decision"]
        for item in status["items"]
    }
    safe: list[MemoryWriteHandoffItem] = []
    skipped: list[dict[str, str]] = []
    for key in sorted(items):
        item = items[key]
        if latest.get(key) not in (None, "pending"):
            skipped.append({"idempotency_key": key, "reason": "already_decided"})
            continue
        risk = _risk_reason(item)
        if risk is not None:
            skipped.append({"idempotency_key": key, "reason": risk})
            continue
        safe.append(item)
    recorded: list[str] = []
    duplicate: list[str] = []
    timestamp = reviewed_at or utc_now()
    for item in safe:
        outcome = record_handoff_decision(
            handoff_dir=handoff_dir,
            handoff_id=handoff_id,
            item=item,
            decision="approve",
            reviewer_id=reviewer_id,
            reason=reason,
            reviewed_at=timestamp,
        )
        if outcome["status"] == "recorded":
            recorded.append(item.idempotency_key)
        else:
            duplicate.append(item.idempotency_key)
    status_value = "recorded" if recorded else ("no_safe_items" if not safe else "already_approved")
    return {
        "status": status_value,
        "handoff_id": handoff_id,
        "recorded": recorded,
        "duplicates": duplicate,
        "skipped": skipped,
        "counts": {
            "safe_items": len(safe),
            "recorded": len(recorded),
            "duplicates": len(duplicate),
            "skipped": len(skipped),
        },
        "changed": bool(recorded),
        "production_writes": False,
        "milvus_written": False,
        "alias_changed": False,
    }
