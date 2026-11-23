from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .handoff_approval import HandoffApproval, handoff_item_sha256
from .handoff_contracts import MemoryWriteHandoffItem
from .handoff_export import HANDOFF_DATA_ARTIFACTS
from .handoff_review import (
    APPROVAL_LEDGER_FILENAME,
    APPROVAL_MANIFEST_FILENAME,
    APPROVED_BATCH_FILENAME,
    read_approval_ledger,
    read_handoff_items,
)
from .security import assert_safe_path, ensure_within, scan_text_for_secrets
from .util import canonical_json, sha256_bytes, sha256_json, sha256_text


class ApprovalExecutionError(ValueError):
    """Raised when an approval execution plan cannot be built safely."""


def _required_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ApprovalExecutionError(f"{field_name} must be a non-empty string")
    return value


def _handoff_dir(path: str | Path) -> Path:
    raw = Path(path).expanduser()
    if raw.is_symlink():
        raise ApprovalExecutionError("handoff directory must not be a symlink")
    directory = assert_safe_path(raw, label="handoff directory")
    if not directory.is_dir():
        raise ApprovalExecutionError("handoff directory must be an existing directory")
    return directory


def _handoff_file(directory: Path, name: str) -> Path:
    path = directory / name
    if path.is_symlink():
        raise ApprovalExecutionError(f"{name} must not be a symlink")
    return ensure_within(path, directory, label=name)


def _read_text(directory: Path, name: str, *, required: bool = True) -> str:
    path = _handoff_file(directory, name)
    if not path.exists():
        if required:
            raise ApprovalExecutionError(f"{name} is required")
        return ""
    if not path.is_file():
        raise ApprovalExecutionError(f"{name} must be a regular file")
    try:
        text = path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ApprovalExecutionError(f"{name} must be UTF-8") from exc
    if scan_text_for_secrets(text):
        raise ApprovalExecutionError(f"{name} failed secret scan")
    return text


def _read_json_object(directory: Path, name: str) -> dict[str, Any]:
    text = _read_text(directory, name)
    try:
        value = json.loads(text)
    except ValueError as exc:
        raise ApprovalExecutionError(f"{name} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ApprovalExecutionError(f"{name} must be a JSON object")
    return value


def _verify_handoff_artifacts(directory: Path, manifest: dict[str, Any]) -> None:
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict) or not artifacts:
        raise ApprovalExecutionError("handoff_manifest artifacts must be a non-empty object")
    for name in HANDOFF_DATA_ARTIFACTS:
        expected = artifacts.get(name)
        if not isinstance(expected, str) or not expected:
            raise ApprovalExecutionError(f"handoff_manifest is missing artifact hash: {name}")
        path = _handoff_file(directory, name)
        if not path.is_file():
            raise ApprovalExecutionError(f"handoff artifact is missing: {name}")
        try:
            actual = sha256_text(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError) as exc:
            raise ApprovalExecutionError(f"handoff artifact is unreadable: {name}") from exc
        if actual != expected:
            raise ApprovalExecutionError(f"handoff artifact hash does not match: {name}")


def _read_manifest_and_items(directory: Path, handoff_id: str) -> tuple[dict[str, Any], dict[str, MemoryWriteHandoffItem]]:
    handoff_manifest = _read_json_object(directory, "handoff_manifest.json")
    if _required_text(handoff_manifest.get("handoff_id"), "handoff_manifest.handoff_id") != _required_text(handoff_id, "handoff_id"):
        raise ApprovalExecutionError("handoff manifest handoff_id does not match")
    _verify_handoff_artifacts(directory, handoff_manifest)
    return handoff_manifest, read_handoff_items(directory)


def _read_approval_artifacts(
    directory: Path,
    *,
    handoff_id: str,
    items: dict[str, MemoryWriteHandoffItem],
) -> tuple[tuple[HandoffApproval, ...], dict[str, MemoryWriteHandoffItem], str, str]:
    approval_manifest = _read_json_object(directory, APPROVAL_MANIFEST_FILENAME)
    if _required_text(approval_manifest.get("handoff_id"), "approval_manifest.handoff_id") != handoff_id:
        raise ApprovalExecutionError("approval manifest handoff_id does not match")
    for field in ("production_writes", "real_llm_called", "milvus_written", "alias_changed"):
        if approval_manifest.get(field) is not False:
            raise ApprovalExecutionError(f"approval manifest safety flag is not false: {field}")

    ledger_text = _read_text(directory, APPROVAL_LEDGER_FILENAME, required=False)
    approved_batch_text = _read_text(directory, APPROVED_BATCH_FILENAME)
    ledger_sha256 = sha256_bytes(_handoff_file(directory, APPROVAL_LEDGER_FILENAME).read_bytes()) if (directory / APPROVAL_LEDGER_FILENAME).exists() else sha256_text("")
    approved_batch_sha256 = sha256_bytes(_handoff_file(directory, APPROVED_BATCH_FILENAME).read_bytes())
    if approval_manifest.get("ledger_sha256") != ledger_sha256:
        raise ApprovalExecutionError("approval manifest ledger_sha256 does not match")
    if approval_manifest.get("approved_batch_sha256") != approved_batch_sha256:
        raise ApprovalExecutionError("approval manifest approved_batch_sha256 does not match")

    approved_items: dict[str, MemoryWriteHandoffItem] = {}
    for line_number, line in enumerate(approved_batch_text.splitlines(), start=1):
        if not line.strip():
            raise ApprovalExecutionError(f"approved batch line {line_number} is empty")
        try:
            raw = json.loads(line)
        except ValueError as exc:
            raise ApprovalExecutionError(f"approved batch line {line_number} is not valid JSON") from exc
        if not isinstance(raw, dict):
            raise ApprovalExecutionError(f"approved batch line {line_number} must be an object")
        try:
            item = MemoryWriteHandoffItem.from_dict(raw)
        except ValueError as exc:
            raise ApprovalExecutionError(f"approved batch line {line_number} failed strict item validation") from exc
        if item.idempotency_key in approved_items:
            raise ApprovalExecutionError("approved batch contains duplicate idempotency keys")
        approved_items[item.idempotency_key] = item

    for key, item in approved_items.items():
        current = items.get(key)
        if current is None:
            raise ApprovalExecutionError("approved batch references an unknown handoff item")
        if item.to_dict() != current.to_dict():
            raise ApprovalExecutionError("approved batch item does not match the current handoff item")

    approvals = read_approval_ledger(directory)
    for approval in approvals:
        if approval.handoff_id != handoff_id:
            raise ApprovalExecutionError("approval ledger record belongs to a different handoff")
        current = items.get(approval.item_idempotency_key)
        if current is None:
            raise ApprovalExecutionError("approval ledger record references an unknown handoff item")
        if approval.item_sha256 != handoff_item_sha256(current):
            raise ApprovalExecutionError("approval ledger item_sha256 does not match the current handoff item")
    return approvals, approved_items, ledger_sha256, approved_batch_sha256


def _latest_decisions(
    approvals: tuple[HandoffApproval, ...],
    *,
    items: dict[str, MemoryWriteHandoffItem],
) -> dict[str, HandoffApproval]:
    latest: dict[str, HandoffApproval] = {}
    for approval in approvals:
        current = latest.get(approval.item_idempotency_key)
        candidate = (datetime.fromisoformat(approval.reviewed_at.replace("Z", "+00:00")), approval.approval_id)
        if current is None or candidate > (
            datetime.fromisoformat(current.reviewed_at.replace("Z", "+00:00")),
            current.approval_id,
        ):
            latest[approval.item_idempotency_key] = approval
    return {key: value for key, value in latest.items() if key in items}


def _item_sort_key(item: MemoryWriteHandoffItem) -> tuple[str, str, int, str]:
    sequence = item.domain_payload.get("source_sequence", -1)
    if isinstance(sequence, bool) or not isinstance(sequence, int):
        sequence = -1
    return (item.conversation_id or "", item.observed_at, sequence, item.idempotency_key)


def _validate_written_memory_map(
    written_memory_map: Mapping[str, Any] | None,
    *,
    items: dict[str, MemoryWriteHandoffItem],
) -> dict[str, dict[str, Any]]:
    if written_memory_map is None:
        raise ApprovalExecutionError("written_memory_map must be explicitly provided")
    if not isinstance(written_memory_map, Mapping):
        raise ApprovalExecutionError("written_memory_map must be an object")
    result: dict[str, dict[str, Any]] = {}
    memory_ids: set[str] = set()
    for key, value in written_memory_map.items():
        if not isinstance(key, str) or key not in items:
            raise ApprovalExecutionError("written_memory_map references an unknown handoff item")
        if not isinstance(value, Mapping):
            raise ApprovalExecutionError("written_memory_map values must be objects")
        if set(value) != {"memory_id", "version"}:
            raise ApprovalExecutionError("written_memory_map values must contain exactly memory_id and version")
        memory_id = _required_text(value["memory_id"], "written_memory_map.memory_id")
        version = value["version"]
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise ApprovalExecutionError("written_memory_map.version must be a positive integer")
        if memory_id in memory_ids:
            raise ApprovalExecutionError("written_memory_map contains duplicate memory_id values")
        memory_ids.add(memory_id)
        result[key] = {"memory_id": memory_id, "version": version}
    return result


def _delete_input(approval: HandoffApproval, *, workspace_id: str, mapping: dict[str, Any]) -> dict[str, Any]:
    memory_id = mapping["memory_id"]
    material = {
        "approval_id": approval.approval_id,
        "workspace_id": workspace_id,
        "target_type": "memory_id",
        "target_id": memory_id,
        "expected_version": mapping["version"],
    }
    digest = sha256_json(material)
    result = {
        "idempotency_key": "mdel_" + digest[:32],
        "operation_id": "op_del_" + digest[:24],
        "workspace_id": workspace_id,
        "target_type": "memory_id",
        "target_id": memory_id,
        "delete_reason": approval.reason,
        "expected_version": mapping["version"],
    }
    if scan_text_for_secrets(canonical_json(result)):
        raise ApprovalExecutionError("post-write revocation failed secret scan")
    return result


def build_approval_execution_plan(
    *,
    handoff_dir: str | Path,
    handoff_id: str,
    written_memory_map: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Build a deterministic database execution plan from independently revalidated approvals."""

    directory = _handoff_dir(handoff_dir)
    expected_handoff_id = _required_text(handoff_id, "handoff_id")
    _, items = _read_manifest_and_items(directory, expected_handoff_id)
    approvals, approved_items, ledger_sha256, approved_batch_sha256 = _read_approval_artifacts(
        directory,
        handoff_id=expected_handoff_id,
        items=items,
    )
    mappings = _validate_written_memory_map(written_memory_map, items=items)
    latest = _latest_decisions(approvals, items=items)
    prior_approve_keys = {
        approval.item_idempotency_key
        for approval in approvals
        if approval.decision == "approve"
    }

    approved_write_items: list[dict[str, Any]] = []
    blocked_items: list[dict[str, Any]] = []
    already_applied_items: list[dict[str, Any]] = []
    post_write_revocations: list[dict[str, Any]] = []
    pending_post_write_revocations: list[dict[str, Any]] = []

    for item in sorted(items.values(), key=_item_sort_key):
        key = item.idempotency_key
        approval = latest.get(key)
        mapping = mappings.get(key)
        if approval is None:
            blocked_items.append({"idempotency_key": key, "decision": "pending", "reason": "no_decision"})
            continue

        if approval.decision == "approve":
            if key not in approved_items:
                raise ApprovalExecutionError("approved batch is missing a current approve item")
            if mapping is not None:
                already_applied_items.append({
                    "idempotency_key": key,
                    "decision": "approve",
                    "status": "already_applied",
                    "approval_id": approval.approval_id,
                    "memory_id": mapping["memory_id"],
                    "version": mapping["version"],
                })
            else:
                approved_write_items.append({"item": item.to_dict(), "approval": approval.to_dict()})
            continue

        if approval.decision == "revoke":
            if mapping is None:
                if key in prior_approve_keys:
                    pending_post_write_revocations.append({
                        "idempotency_key": key,
                        "approval_id": approval.approval_id,
                        "item_sha256": handoff_item_sha256(item),
                    })
                else:
                    blocked_items.append({
                        "idempotency_key": key,
                        "decision": "revoke",
                        "reason": "revoked_before_write",
                    })
            else:
                post_write_revocations.append({
                    "idempotency_key": key,
                    "approval_id": approval.approval_id,
                    "delete_input": _delete_input(approval, workspace_id=item.workspace_id, mapping=mapping),
                })
            continue

        blocked_items.append({
            "idempotency_key": key,
            "decision": approval.decision,
            "reason": f"latest_decision_{approval.decision}",
        })

    counts = {
        "items": len(items),
        "approved_write_items": len(approved_write_items),
        "already_applied_items": len(already_applied_items),
        "blocked_items": len(blocked_items),
        "post_write_revocations": len(post_write_revocations),
        "pending_post_write_revocations": len(pending_post_write_revocations),
    }
    return {
        "handoff_id": expected_handoff_id,
        "ledger_sha256": ledger_sha256,
        "approved_batch_sha256": approved_batch_sha256,
        "approved_write_items": approved_write_items,
        "blocked_items": blocked_items,
        "already_applied_items": already_applied_items,
        "post_write_revocations": post_write_revocations,
        "pending_post_write_revocations": pending_post_write_revocations,
        "counts": counts,
        "production_writes": False,
        "real_llm_called": False,
        "milvus_written": False,
        "alias_changed": False,
    }
