from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .handoff_approval import (
    APPROVAL_CONTRACT_VERSION,
    HandoffApproval,
    HandoffApprovalError,
    create_handoff_approval,
    handoff_item_sha256,
)
from .handoff_contracts import MemoryWriteHandoffItem
from .security import assert_safe_path, ensure_within, scan_text_for_secrets
from .util import atomic_write_text, canonical_json, jsonl, pretty_json, sha256_bytes, sha256_text


APPROVAL_LEDGER_FILENAME = "approval_ledger.jsonl"
APPROVED_BATCH_FILENAME = "approved_memory_write_batch.jsonl"
APPROVAL_MANIFEST_FILENAME = "approval_manifest.json"
APPROVAL_REVIEW_FILENAME = "APPROVAL_REVIEW.md"
_APPROVAL_OUTPUTS = (
    APPROVED_BATCH_FILENAME,
    APPROVAL_MANIFEST_FILENAME,
    APPROVAL_REVIEW_FILENAME,
)
_DECISION_COUNT_NAMES = {
    "approve": "approved",
    "reject": "rejected",
    "defer": "deferred",
    "revoke": "revoked",
}


class HandoffReviewError(ValueError):
    """Raised when approval ledger or approved-batch data is invalid."""


def _required_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HandoffReviewError(f"{field_name} must be a non-empty string")
    return value


def _handoff_dir(path: str | Path) -> Path:
    raw = Path(path).expanduser()
    if raw.is_symlink():
        raise HandoffReviewError("handoff directory must not be a symlink")
    directory = assert_safe_path(raw, label="handoff directory")
    if not directory.is_dir():
        raise HandoffReviewError("handoff directory must be an existing directory")
    return directory


def _handoff_file(directory: Path, name: str) -> Path:
    path = directory / name
    if path.is_symlink():
        raise HandoffReviewError(f"{name} must not be a symlink")
    return ensure_within(path, directory, label=name)


def _read_json_object(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HandoffReviewError(f"{label} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise HandoffReviewError(f"{label} must be a JSON object")
    return value


def _read_handoff_manifest(directory: Path) -> dict[str, Any]:
    path = _handoff_file(directory, "handoff_manifest.json")
    if not path.is_file():
        raise HandoffReviewError("handoff_manifest.json is required")
    manifest = _read_json_object(path, label="handoff_manifest.json")
    _required_text(manifest.get("handoff_id"), "handoff_manifest.handoff_id")
    return manifest


def _read_handoff_items(directory: Path) -> dict[str, MemoryWriteHandoffItem]:
    path = _handoff_file(directory, "memory_write_batch.jsonl")
    if not path.is_file():
        raise HandoffReviewError("memory_write_batch.jsonl is required")
    try:
        text = path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise HandoffReviewError("memory_write_batch.jsonl must be UTF-8") from exc
    if scan_text_for_secrets(text):
        raise HandoffReviewError("memory_write_batch.jsonl failed secret scan")
    items: dict[str, MemoryWriteHandoffItem] = {}
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            raise HandoffReviewError(f"memory_write_batch.jsonl line {line_number} is empty")
        try:
            raw = json.loads(line)
        except ValueError as exc:
            raise HandoffReviewError(f"memory_write_batch.jsonl line {line_number} is not valid JSON") from exc
        if not isinstance(raw, dict):
            raise HandoffReviewError(f"memory_write_batch.jsonl line {line_number} must be an object")
        try:
            item = MemoryWriteHandoffItem.from_dict(raw)
        except ValueError as exc:
            raise HandoffReviewError(f"memory_write_batch.jsonl line {line_number} failed strict item validation") from exc
        if item.idempotency_key in items:
            raise HandoffReviewError("memory_write_batch.jsonl contains duplicate idempotency keys")
        items[item.idempotency_key] = item
    return items


def read_handoff_items(handoff_dir: str | Path) -> dict[str, MemoryWriteHandoffItem]:
    """Read the current proposal batch with strict handoff-item validation."""

    return _read_handoff_items(_handoff_dir(handoff_dir))


def read_approval_ledger(handoff_dir: str | Path) -> tuple[HandoffApproval, ...]:
    """Read a strict ledger without accepting damaged or unknown records."""

    directory = _handoff_dir(handoff_dir)
    path = _handoff_file(directory, APPROVAL_LEDGER_FILENAME)
    if not path.exists():
        return ()
    if not path.is_file():
        raise HandoffReviewError("approval_ledger.jsonl must be a regular file")
    try:
        text = path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise HandoffReviewError("approval_ledger.jsonl must be UTF-8") from exc
    if scan_text_for_secrets(text):
        raise HandoffReviewError("approval_ledger.jsonl failed secret scan")
    records: list[HandoffApproval] = []
    by_approval_id: dict[str, HandoffApproval] = {}
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            raise HandoffReviewError(f"approval_ledger.jsonl line {line_number} is empty")
        try:
            raw = json.loads(line)
        except ValueError as exc:
            raise HandoffReviewError(f"approval_ledger.jsonl line {line_number} is not valid JSON") from exc
        if not isinstance(raw, dict):
            raise HandoffReviewError(f"approval_ledger.jsonl line {line_number} must be an object")
        try:
            approval = HandoffApproval.from_dict(raw)
        except HandoffApprovalError as exc:
            raise HandoffReviewError(f"approval_ledger.jsonl line {line_number} failed strict approval validation") from exc
        existing = by_approval_id.get(approval.approval_id)
        if existing is not None:
            if existing.to_dict() != approval.to_dict():
                raise HandoffReviewError("approval_ledger.jsonl contains a conflicting duplicate approval_id")
            continue
        by_approval_id[approval.approval_id] = approval
        records.append(approval)
    _validate_temporal_conflicts(records)
    return tuple(records)


def _approval_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _approval_time_key(value: str) -> datetime:
    return _approval_time(value).astimezone(timezone.utc)


def _validate_temporal_conflicts(records: list[HandoffApproval]) -> None:
    by_item_time: dict[tuple[str, str, str, datetime], str] = {}
    for approval in records:
        key = (
            approval.handoff_id,
            approval.item_idempotency_key,
            approval.item_sha256,
            _approval_time_key(approval.reviewed_at),
        )
        existing = by_item_time.get(key)
        if existing is not None and existing != approval.decision:
            raise HandoffReviewError("approval_ledger.jsonl has conflicting decisions at the same reviewed_at")
        by_item_time[key] = approval.decision


def _validate_ledger_against_items(
    approvals: tuple[HandoffApproval, ...],
    items: dict[str, MemoryWriteHandoffItem],
    *,
    handoff_id: str,
) -> None:
    for approval in approvals:
        if approval.handoff_id != handoff_id:
            raise HandoffReviewError("approval ledger record belongs to a different handoff")
        item = items.get(approval.item_idempotency_key)
        if item is None:
            raise HandoffReviewError("approval ledger record references an unknown handoff item")
        if approval.item_sha256 != handoff_item_sha256(item):
            raise HandoffReviewError("approval ledger item_sha256 does not match the current handoff item")


def _latest_decisions(
    approvals: tuple[HandoffApproval, ...],
    items: dict[str, MemoryWriteHandoffItem],
) -> dict[str, HandoffApproval]:
    latest: dict[str, HandoffApproval] = {}
    for approval in approvals:
        current = latest.get(approval.item_idempotency_key)
        candidate_key = (_approval_time(approval.reviewed_at), approval.approval_id)
        if current is None or candidate_key > (_approval_time(current.reviewed_at), current.approval_id):
            latest[approval.item_idempotency_key] = approval
    return {key: value for key, value in latest.items() if key in items}


def _item_sort_key(item: MemoryWriteHandoffItem) -> tuple[str, str, int, str]:
    sequence = item.domain_payload.get("source_sequence", -1)
    if isinstance(sequence, bool) or not isinstance(sequence, int):
        sequence = -1
    return (item.conversation_id or "", item.observed_at, sequence, item.idempotency_key)


def _decision_counts(items: dict[str, MemoryWriteHandoffItem], latest: dict[str, HandoffApproval]) -> dict[str, int]:
    counts = {"approved": 0, "rejected": 0, "deferred": 0, "revoked": 0, "pending": 0}
    for key in items:
        approval = latest.get(key)
        if approval is None:
            counts["pending"] += 1
        else:
            counts[_DECISION_COUNT_NAMES[approval.decision]] += 1
    return counts


def _render_batch(items: dict[str, MemoryWriteHandoffItem], latest: dict[str, HandoffApproval]) -> tuple[str, list[MemoryWriteHandoffItem]]:
    approved = [
        item
        for key, item in items.items()
        if latest.get(key) is not None and latest[key].decision == "approve"
    ]
    approved.sort(key=_item_sort_key)
    return jsonl([item.to_dict() for item in approved]), approved


def _render_manifest(
    *,
    handoff_id: str,
    ledger_sha256: str,
    batch_text: str,
    counts: dict[str, int],
    item_count: int,
) -> str:
    manifest = {
        "approval_contract_version": APPROVAL_CONTRACT_VERSION,
        "handoff_id": handoff_id,
        "ledger_sha256": ledger_sha256,
        "approved_batch_sha256": sha256_text(batch_text),
        "item_count": item_count,
        "approved_count": counts["approved"],
        "rejected_count": counts["rejected"],
        "deferred_count": counts["deferred"],
        "revoked_count": counts["revoked"],
        "pending_count": counts["pending"],
        "production_writes": False,
        "real_llm_called": False,
        "milvus_written": False,
        "alias_changed": False,
    }
    return pretty_json(manifest) + "\n"


def _markdown_cell(value: str | None) -> str:
    return " ".join((value or "").split()).replace("|", "\\|")


def _render_review_markdown(
    *,
    handoff_id: str,
    items: dict[str, MemoryWriteHandoffItem],
    latest: dict[str, HandoffApproval],
    counts: dict[str, int],
) -> str:
    lines = [
        "# Handoff Approval Review",
        "",
        "> Suggestion-only approval review. No canonical database, Milvus, or alias write has occurred.",
        "",
        f"- handoff_id: `{handoff_id}`",
        f"- items: `{len(items)}`",
        "",
        "| count | value |",
        "|---|---:|",
        f"| approved | {counts['approved']} |",
        f"| rejected | {counts['rejected']} |",
        f"| deferred | {counts['deferred']} |",
        f"| revoked | {counts['revoked']} |",
        f"| pending | {counts['pending']} |",
        "",
        "| item idempotency key | latest decision | reviewer | reason | reviewed_at | hash status |",
        "|---|---|---|---|---|---|",
    ]
    for item in sorted(items.values(), key=_item_sort_key):
        approval = latest.get(item.idempotency_key)
        if approval is None:
            decision = "pending"
            reviewer = ""
            reason = ""
            reviewed_at = ""
            hash_status = f"item {handoff_item_sha256(item)[:16]}...; no decision"
        else:
            decision = approval.decision
            reviewer = approval.reviewer_id
            reason = approval.reason
            reviewed_at = approval.reviewed_at
            hash_status = f"matched {approval.item_sha256[:16]}..."
        lines.append(
            f"| `{_markdown_cell(item.idempotency_key)}` | `{decision}` | {_markdown_cell(reviewer)} | "
            f"{_markdown_cell(reason)} | {_markdown_cell(reviewed_at)} | {_markdown_cell(hash_status)} |"
        )
    lines.extend([
        "",
        "Approval records authorize review of a proposed handoff. They do not prove persistence, authorization, version success, or a canonical write.",
        "",
    ])
    return "\n".join(lines)


def _read_ledger_raw(directory: Path) -> tuple[bytes, str]:
    path = _handoff_file(directory, APPROVAL_LEDGER_FILENAME)
    if not path.exists():
        return b"", ""
    try:
        raw = path.read_bytes()
        text = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise HandoffReviewError("approval_ledger.jsonl must be UTF-8") from exc
    return raw, text


def record_handoff_decision(
    *,
    handoff_dir: str | Path,
    handoff_id: str,
    item: MemoryWriteHandoffItem | Mapping[str, Any],
    decision: str,
    reviewer_id: str,
    reason: str,
    reviewed_at: str,
) -> dict[str, Any]:
    """Append a decision to the handoff ledger; exact duplicates are idempotent."""

    directory = _handoff_dir(handoff_dir)
    wanted_handoff_id = _required_text(handoff_id, "handoff_id")
    manifest = _read_handoff_manifest(directory)
    if manifest["handoff_id"] != wanted_handoff_id:
        raise HandoffReviewError("handoff_id does not match the handoff directory manifest")
    items = _read_handoff_items(directory)
    if not isinstance(item, MemoryWriteHandoffItem):
        try:
            item = MemoryWriteHandoffItem.from_dict(item)
        except ValueError as exc:
            raise HandoffReviewError("handoff item failed strict contract validation") from exc
    current = items.get(item.idempotency_key)
    if current is None:
        raise HandoffReviewError("handoff item is not present in the current handoff batch")
    if current.to_dict() != item.to_dict():
        raise HandoffReviewError("handoff item does not match the current handoff batch item")

    try:
        approval = create_handoff_approval(
            item=item,
            handoff_id=wanted_handoff_id,
            decision=decision,
            reviewer_id=reviewer_id,
            reason=reason,
            reviewed_at=reviewed_at,
        )
    except HandoffApprovalError as exc:
        raise HandoffReviewError(f"approval record is invalid: {exc}") from exc
    existing_raw, existing_text = _read_ledger_raw(directory)
    existing = read_approval_ledger(directory)
    _validate_ledger_against_items(existing, items, handoff_id=wanted_handoff_id)
    for record in existing:
        if record.to_dict() == approval.to_dict():
            return {
                "status": "duplicate",
                "changed": False,
                "approval": approval.to_dict(),
                "ledger_sha256": sha256_bytes(existing_raw),
            }
        if (
            record.handoff_id == approval.handoff_id
            and record.item_idempotency_key == approval.item_idempotency_key
            and record.item_sha256 == approval.item_sha256
            and _approval_time_key(record.reviewed_at) == _approval_time_key(approval.reviewed_at)
            and record.decision != approval.decision
        ):
            raise HandoffReviewError("approval ledger has a conflicting decision at the same reviewed_at")

    approval_line = canonical_json(approval.to_dict()) + "\n"
    if scan_text_for_secrets(approval_line):
        raise HandoffReviewError("approval decision failed secret scan")
    path = _handoff_file(directory, APPROVAL_LEDGER_FILENAME)
    if existing_text and not existing_text.endswith("\n"):
        existing_text += "\n"
    new_text = existing_text + approval_line
    atomic_write_text(path, new_text)
    return {
        "status": "recorded",
        "changed": True,
        "approval": approval.to_dict(),
        "ledger_sha256": sha256_text(new_text),
    }


def handoff_approval_status(*, handoff_dir: str | Path, handoff_id: str | None = None) -> dict[str, Any]:
    """Return the current latest decisions without writing any file."""

    directory = _handoff_dir(handoff_dir)
    manifest = _read_handoff_manifest(directory)
    actual_handoff_id = manifest["handoff_id"]
    if handoff_id is not None and _required_text(handoff_id, "handoff_id") != actual_handoff_id:
        raise HandoffReviewError("handoff_id does not match the handoff directory manifest")
    items = _read_handoff_items(directory)
    ledger_raw, _ = _read_ledger_raw(directory)
    approvals = read_approval_ledger(directory)
    _validate_ledger_against_items(approvals, items, handoff_id=actual_handoff_id)
    latest = _latest_decisions(approvals, items)
    counts = _decision_counts(items, latest)
    batch_path = _handoff_file(directory, APPROVED_BATCH_FILENAME)
    decisions: list[dict[str, Any]] = []
    for item in sorted(items.values(), key=_item_sort_key):
        approval = latest.get(item.idempotency_key)
        decisions.append({
            "idempotency_key": item.idempotency_key,
            "latest_decision": approval.decision if approval is not None else "pending",
            "reviewer_id": approval.reviewer_id if approval is not None else None,
            "reviewed_at": approval.reviewed_at if approval is not None else None,
            "approval_id": approval.approval_id if approval is not None else None,
            "item_sha256": handoff_item_sha256(item),
        })
    return {
        "handoff_id": actual_handoff_id,
        "ledger_sha256": sha256_bytes(ledger_raw),
        "ledger_record_count": len(approvals),
        "item_count": len(items),
        "counts": counts,
        "items": decisions,
        "approved_batch_exists": batch_path.is_file(),
    }


def generate_approved_batch(*, handoff_dir: str | Path, handoff_id: str | None = None) -> dict[str, Any]:
    """Generate the current approved batch and deterministic approval review artifacts."""

    directory = _handoff_dir(handoff_dir)
    manifest = _read_handoff_manifest(directory)
    actual_handoff_id = manifest["handoff_id"]
    if handoff_id is not None and _required_text(handoff_id, "handoff_id") != actual_handoff_id:
        raise HandoffReviewError("handoff_id does not match the handoff directory manifest")
    items = _read_handoff_items(directory)
    ledger_raw, _ = _read_ledger_raw(directory)
    approvals = read_approval_ledger(directory)
    _validate_ledger_against_items(approvals, items, handoff_id=actual_handoff_id)
    latest = _latest_decisions(approvals, items)
    counts = _decision_counts(items, latest)
    batch_text, approved_items = _render_batch(items, latest)
    review_text = _render_review_markdown(
        handoff_id=actual_handoff_id,
        items=items,
        latest=latest,
        counts=counts,
    )
    manifest_text = _render_manifest(
        handoff_id=actual_handoff_id,
        ledger_sha256=sha256_bytes(ledger_raw),
        batch_text=batch_text,
        counts=counts,
        item_count=len(items),
    )
    rendered = {
        APPROVED_BATCH_FILENAME: batch_text,
        APPROVAL_MANIFEST_FILENAME: manifest_text,
        APPROVAL_REVIEW_FILENAME: review_text,
    }
    paths = {name: _handoff_file(directory, name) for name in _APPROVAL_OUTPUTS}
    existing = {
        name: path.read_text(encoding="utf-8") if path.is_file() else None
        for name, path in paths.items()
    }
    unchanged = all(existing[name] == rendered[name] for name in _APPROVAL_OUTPUTS)
    if not unchanged:
        for name in _APPROVAL_OUTPUTS:
            atomic_write_text(paths[name], rendered[name])
    return {
        "status": "already_generated" if unchanged else "generated",
        "changed": not unchanged,
        "handoff_id": actual_handoff_id,
        "counts": counts,
        "ledger_sha256": sha256_bytes(ledger_raw),
        "approved_batch_sha256": sha256_text(batch_text),
        "output_paths": {name: str(path) for name, path in paths.items()},
    }
