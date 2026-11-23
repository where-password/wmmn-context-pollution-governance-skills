from __future__ import annotations

import ast
import hashlib
import json
import os
from datetime import datetime
from pathlib import Path

import pytest

from memory_core.models import MemoryDeleteInput, MemoryWriteInput
from memory_core.skill_api import SkillApiBlocked, plan_handoff_ingest

HANDOFF_ID = "handoff_database_side_test"
WORKSPACE_ID = "ws_handoff"
PROJECT_ID = "project_handoff"
DOMAIN_PROFILE = "generic"
PROFILE_VERSION = "1.0.0"
SOURCE_ID = "source_handoff"
CONVERSATION_ID = "conv_handoff"
BASE_REVIEWED_AT = "2026-11-22T10:00:00+08:00"


def _canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha(value: bytes | str) -> str:
    if isinstance(value, str):
        value = value.encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def _item(*, key: str = "mwh_test_1", content: str = "Database handoff approved fact.", conversation_id: str = CONVERSATION_ID, supersedes_ref: str | None = None, expected_version: int | None = None, extra: dict | None = None) -> dict:
    value = {
        "idempotency_key": key,
        "operation_id": "op_" + key,
        "workspace_id": WORKSPACE_ID,
        "project_id": PROJECT_ID,
        "task_id": None,
        "conversation_id": conversation_id,
        "domain_profile": DOMAIN_PROFILE,
        "profile_version": PROFILE_VERSION,
        "memory_type": "fact",
        "subtype": "fact",
        "subject": "handoff subject",
        "entity_type": "topic",
        "entity_id": "topic-1",
        "content": content,
        "importance": "medium",
        "declared_sensitivity": "private",
        "requested_visibility": "private",
        "assertion_source": "user_report",
        "verification_state": "quote_verified",
        "effective_from": None,
        "effective_to": None,
        "source_id": SOURCE_ID,
        "source_unit_id": "su_" + key,
        "source_scope": {
            "scope_type": "conversation_message",
            "ref": "msg_" + key,
            "start": 0,
            "end": len(content),
            "locator_json": {"message_id": "msg_" + key},
        },
        "confidence": 0.9,
        "supersedes_ref": supersedes_ref,
        "supersedes_id": None,
        "expected_version": expected_version,
        "domain_payload": {"source_sequence": 1},
        "evidences": [{
            "evidence_id": "hev_" + key,
            "source_unit_id": "su_" + key,
            "quote": content,
            "locator_json": {"message_id": "msg_" + key, "char_start": 0, "char_end": len(content)},
            "status": "verified",
        }],
        "claim_key": "claim:" + key,
        "scope_key": f"workspace:{WORKSPACE_ID}|project:{PROJECT_ID}",
        "observed_at": "2026-11-22T09:00:00+08:00",
        "transition_kind": "supersedes" if supersedes_ref else "new",
    }
    if extra:
        value.update(extra)
    return value


def _rewrite_source_units(directory: Path, units: list[dict], *, reseal: bool = True) -> None:
    payload = "".join(
        json.dumps(unit, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        for unit in units
    )
    path = directory / "source_units.jsonl"
    path.write_text(payload, encoding="utf-8", newline="")
    if reseal:
        manifest_path = directory / "handoff_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["artifacts"]["source_units.jsonl"] = _sha(payload)
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )


def _approval(item: dict, decision: str, reviewed_at: str, *, handoff_id: str = HANDOFF_ID, reviewer: str = "reviewer-1") -> dict:
    material = {
        "approval_contract_version": "conversation-memory-handoff-approval-v1",
        "handoff_id": handoff_id,
        "item_idempotency_key": item["idempotency_key"],
        "item_sha256": _sha(_canonical(item)),
        "decision": decision,
        "reviewer_id": reviewer,
        "reason": f"Controlled {decision} for handoff test.",
        "reviewed_at": reviewed_at,
    }
    return {"approval_id": "hap_" + _sha(_canonical(material))[:32], **material}


def _latest(approvals: list[dict]) -> dict[str, dict]:
    latest: dict[str, dict] = {}
    for approval in approvals:
        key = approval["item_idempotency_key"]
        current = latest.get(key)
        if current is None or (
            datetime.fromisoformat(approval["reviewed_at"].replace("Z", "+00:00")),
            approval["approval_id"],
        ) > (
            datetime.fromisoformat(current["reviewed_at"].replace("Z", "+00:00")),
            current["approval_id"],
        ):
            latest[key] = approval
    return latest


def _source_unit(item: dict, *, sequence: int | None = None) -> dict:
    evidence = item["evidences"][0]
    message_id = evidence["locator_json"].get("message_id") or item["source_scope"]["ref"]
    visibility = "private" if item.get("requested_visibility", "private") == "private" else "workspace"
    return {
        "source_id": item["source_id"],
        "workspace_id": item["workspace_id"],
        "project_id": item["project_id"],
        "source_unit_id": item["source_unit_id"],
        "parent_unit_id": None,
        "source_unit_hash": _sha(item["content"]),
        "scope_type": item["source_scope"]["scope_type"],
        "scope_ref": item["source_scope"]["ref"],
        "text_content": item["content"],
        "conversation_id": item.get("conversation_id"),
        "message_id": message_id,
        "role": "user",
        "sequence": sequence if sequence is not None else int(item.get("domain_payload", {}).get("source_sequence", 0)),
        "timestamp": item["observed_at"],
        "source_type": "conversation_handoff_test",
        "source_path": f"memory://{item['source_id']}",
        "source_sha256": "1" * 64,
        "visibility": visibility,
        "sensitivity": item.get("declared_sensitivity", "private"),
        "metadata": {"handoff_id": HANDOFF_ID},
    }


def _write_fixture(tmp_path: Path, items: list[dict], decisions: list[tuple[dict, str, str]], *, conversation_ids: list[str] | None = None) -> tuple[Path, dict[str, str]]:
    directory = tmp_path / "handoff"
    directory.mkdir(parents=True)
    item_lines = "".join(json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n" for item in items)
    source_units_by_id: dict[str, dict] = {}
    for item in items:
        unit = _source_unit(item)
        source_units_by_id[unit["source_unit_id"]] = unit
    source_units_lines = "".join(
        json.dumps(unit, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        for unit in sorted(source_units_by_id.values(), key=lambda row: (row["source_id"], row["sequence"], row["source_unit_id"]))
    )
    artifacts = {
        "memory_write_batch.jsonl": item_lines,
        "source_units.jsonl": source_units_lines,
        "state_transitions.jsonl": json.dumps({"handoff_id": HANDOFF_ID, "transitions": []}, sort_keys=True) + "\n",
        "review_pending.jsonl": "",
        "HANDOFF_REVIEW.md": "# Handoff Review\n\nSynthetic database-side test artifact.\n",
    }
    for name, content in artifacts.items():
        (directory / name).write_text(content, encoding="utf-8", newline="")
    approvals = [_approval(item, decision, reviewed_at) for item, decision, reviewed_at in decisions]
    latest = _latest(approvals)
    approved_items = [item for item in items if latest.get(item["idempotency_key"], {}).get("decision") == "approve"]
    approved_batch = "".join(json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n" for item in approved_items)
    ledger = "".join(json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n" for record in approvals)
    (directory / "approval_ledger.jsonl").write_text(ledger, encoding="utf-8", newline="")
    (directory / "approved_memory_write_batch.jsonl").write_text(approved_batch, encoding="utf-8", newline="")
    counts = {"approved": 0, "rejected": 0, "deferred": 0, "revoked": 0, "pending": 0}
    decision_names = {"approve": "approved", "reject": "rejected", "defer": "deferred", "revoke": "revoked"}
    for item in items:
        decision = latest.get(item["idempotency_key"], {}).get("decision")
        counts[decision_names[decision] if decision else "pending"] += 1
    approval_manifest = {
        "approval_contract_version": "conversation-memory-handoff-approval-v1",
        "handoff_id": HANDOFF_ID,
        "ledger_sha256": _sha(ledger),
        "approved_batch_sha256": _sha(approved_batch),
        "item_count": len(items),
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
    approval_manifest_text = json.dumps(approval_manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    (directory / "approval_manifest.json").write_text(approval_manifest_text, encoding="utf-8", newline="")
    handoff_manifest = {
        "handoff_contract": "conversation-memory-handoff",
        "handoff_contract_version": "conversation-memory-handoff-v1",
        "handoff_id": HANDOFF_ID,
        "created_at": "2026-11-22T11:00:00+08:00",
        "run_id": "run_handoff_test",
        "plan_id": "plan_handoff_test",
        "source_sha256": "1" * 64,
        "observations_sha256": "2" * 64,
        "workspace_id": WORKSPACE_ID,
        "project_id": PROJECT_ID,
        "domain_profile": DOMAIN_PROFILE,
        "profile_version": PROFILE_VERSION,
        "source_id": SOURCE_ID,
        "conversation_ids": conversation_ids or sorted({item["conversation_id"] for item in items if item.get("conversation_id")}),
        "counts": {"write_items": len(items)},
        "artifacts": {name: _sha(content) for name, content in artifacts.items()},
        "approval_manifest_sha256": _sha(approval_manifest_text),
        "review_only": True,
        "production_writes": False,
        "canonical_writes": False,
        "real_llm_called": False,
        "milvus_written": False,
        "alias_changed": False,
        "contains_security_fields": False,
    }
    (directory / "handoff_manifest.json").write_text(
        json.dumps(handoff_manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
        newline="",
    )
    return directory, handoff_manifest


def _plan(directory: Path, written_memory_map=None):
    return plan_handoff_ingest(
        handoff_dir=directory,
        handoff_id=HANDOFF_ID,
        workspace_id=WORKSPACE_ID,
        project_id=PROJECT_ID,
        domain_profile=DOMAIN_PROFILE,
        profile_version=PROFILE_VERSION,
        source_id=SOURCE_ID,
        written_memory_map={} if written_memory_map is None else written_memory_map,
    )


def test_approve_generates_one_strict_memory_write_input(tmp_path: Path) -> None:
    item = _item()
    directory, _ = _write_fixture(tmp_path, [item], [(item, "approve", BASE_REVIEWED_AT)])
    plan = _plan(directory)
    assert plan.counts.writes == 1
    assert plan.counts.blocked_items == 0
    assert isinstance(plan.writes[0], MemoryWriteInput)
    write = plan.writes[0]
    assert write.idempotency_key == item["idempotency_key"]
    assert write.domain_payload["conversation_handoff"]["approval_id"].startswith("hap_")
    assert write.domain_payload["conversation_handoff"]["handoff_id"] == HANDOFF_ID
    assert write.evidences[0].status == "verified"
    assert write.evidences[0].quote == item["evidences"][0]["quote"]


def test_latest_approve_is_the_only_write_decision(tmp_path: Path) -> None:
    item = _item()
    directory, _ = _write_fixture(
        tmp_path,
        [item],
        [
            (item, "defer", "2026-11-22T09:00:00+08:00"),
            (item, "approve", "2026-11-22T10:00:00+08:00"),
            (item, "defer", "2026-11-22T09:30:00+08:00"),
        ],
    )
    plan = _plan(directory)
    assert plan.counts.writes == 1
    assert plan.counts.blocked_items == 0


def test_revoke_with_written_mapping_generates_real_delete_input(tmp_path: Path) -> None:
    item = _item()
    directory, _ = _write_fixture(
        tmp_path,
        [item],
        [(item, "approve", "2026-11-22T09:00:00+08:00"), (item, "revoke", "2026-11-22T10:00:00+08:00")],
    )
    plan = _plan(directory, {item["idempotency_key"]: {"memory_id": "mem_written_1", "version": 3}})
    assert plan.counts.deletes == 1
    assert plan.counts.writes == 0
    assert isinstance(plan.deletes[0], MemoryDeleteInput)
    assert plan.deletes[0].target_id == "mem_written_1"
    assert plan.deletes[0].expected_version == 3


def test_revoke_without_mapping_enters_pending_post_write_revocation(tmp_path: Path) -> None:
    item = _item()
    directory, _ = _write_fixture(
        tmp_path,
        [item],
        [(item, "approve", "2026-11-22T09:00:00+08:00"), (item, "revoke", "2026-11-22T10:00:00+08:00")],
    )
    plan = _plan(directory, {})
    assert plan.counts.pending_post_write_revocations == 1
    assert plan.counts.deletes == 0
    assert plan.pending_post_write_revocations[0].idempotency_key == item["idempotency_key"]


def test_unapproved_reject_and_defer_are_blocked_not_written(tmp_path: Path) -> None:
    items = [_item(key="mwh_pending"), _item(key="mwh_reject"), _item(key="mwh_defer")]
    directory, _ = _write_fixture(
        tmp_path,
        items,
        [(items[1], "reject", BASE_REVIEWED_AT), (items[2], "defer", BASE_REVIEWED_AT)],
    )
    plan = _plan(directory)
    assert plan.counts.writes == 0
    assert plan.counts.blocked_items == 3
    reasons = {row.idempotency_key: row.reason for row in plan.blocked_items}
    assert reasons["mwh_pending"] == "no_decision"
    assert reasons["mwh_reject"] == "latest_decision_reject"
    assert reasons["mwh_defer"] == "latest_decision_defer"


def test_already_applied_approve_is_not_rewritten(tmp_path: Path) -> None:
    item = _item()
    directory, _ = _write_fixture(tmp_path, [item], [(item, "approve", BASE_REVIEWED_AT)])
    plan = _plan(directory, {item["idempotency_key"]: {"memory_id": "mem_existing", "version": 2}})
    assert plan.counts.writes == 0
    assert plan.counts.already_applied == 1
    assert plan.already_applied[0].memory_id == "mem_existing"


def test_supersedes_ref_requires_explicit_map_and_uses_its_version(tmp_path: Path) -> None:
    old = _item(key="mwh_old")
    new = _item(key="mwh_new", supersedes_ref=old["idempotency_key"])
    directory, _ = _write_fixture(
        tmp_path,
        [old, new],
        [(old, "approve", "2026-11-22T09:00:00+08:00"), (new, "approve", "2026-11-22T10:00:00+08:00")],
    )
    plan = _plan(directory, {old["idempotency_key"]: {"memory_id": "mem_old", "version": 4}})
    assert [write.idempotency_key for write in plan.writes] == ["mwh_new"]
    assert plan.writes[0].supersedes_id == "mem_old"
    assert plan.writes[0].expected_version == 4

    with pytest.raises(SkillApiBlocked, match="supersedes_ref"):
        _plan(directory, {})


def test_artifact_and_approval_hash_tampering_fails_closed(tmp_path: Path) -> None:
    item = _item()
    directory, _ = _write_fixture(tmp_path, [item], [(item, "approve", BASE_REVIEWED_AT)])
    artifact = directory / "memory_write_batch.jsonl"
    artifact.write_text(artifact.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(SkillApiBlocked, match="artifact hash"):
        _plan(directory)

    directory, _ = _write_fixture(tmp_path / "ledger-tamper", [item], [(item, "approve", BASE_REVIEWED_AT)])
    ledger = directory / "approval_ledger.jsonl"
    ledger.write_text(ledger.read_text(encoding="utf-8").replace("reviewer-1", "reviewer-X"), encoding="utf-8")
    with pytest.raises(SkillApiBlocked, match="artifact hash"):
        _plan(directory)

    directory, _ = _write_fixture(tmp_path / "batch-tamper", [item], [(item, "approve", BASE_REVIEWED_AT)])
    batch = directory / "approved_memory_write_batch.jsonl"
    batch.write_text(batch.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(SkillApiBlocked, match="artifact hash"):
        _plan(directory)

    directory, _ = _write_fixture(tmp_path / "approval-manifest-tamper", [item], [(item, "approve", BASE_REVIEWED_AT)])
    approval_manifest = directory / "approval_manifest.json"
    approval_manifest.write_text(approval_manifest.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(SkillApiBlocked, match="approval_manifest_sha256"):
        _plan(directory)


def test_source_units_are_planned_and_strictly_cross_validated(tmp_path: Path) -> None:
    item = _item()
    directory, _ = _write_fixture(tmp_path / "valid", [item], [(item, "approve", BASE_REVIEWED_AT)])
    plan = _plan(directory)
    assert plan.source_registration_counts.sources == 1
    assert plan.source_registration_counts.source_units == 1
    assert plan.source_units[0].source_unit_id == item["source_unit_id"]
    assert plan.source_units[0].text_content == item["content"]

    quote_mismatch = _source_unit(item)
    quote_mismatch["text_content"] = "Different source text."
    quote_mismatch["source_unit_hash"] = _sha(quote_mismatch["text_content"])
    directory, _ = _write_fixture(tmp_path / "quote", [item], [(item, "approve", BASE_REVIEWED_AT)])
    _rewrite_source_units(directory, [quote_mismatch])
    with pytest.raises(SkillApiBlocked, match="exact substring"):
        _plan(directory)

    identity_hash = _source_unit(item)
    identity_hash["source_unit_hash"] = "f" * 64
    directory, _ = _write_fixture(tmp_path / "identity-hash", [item], [(item, "approve", BASE_REVIEWED_AT)])
    _rewrite_source_units(directory, [identity_hash])
    identity_plan = _plan(directory)
    assert identity_plan.source_units[0].source_unit_hash == "f" * 64

    invalid_hash = _source_unit(item)
    invalid_hash["source_unit_hash"] = "not-a-sha256"
    directory, _ = _write_fixture(tmp_path / "invalid-hash", [item], [(item, "approve", BASE_REVIEWED_AT)])
    _rewrite_source_units(directory, [invalid_hash])
    with pytest.raises(SkillApiBlocked):
        _plan(directory)

    unknown_field = _source_unit(item)
    unknown_field["unexpected_field"] = "blocked"
    directory, _ = _write_fixture(tmp_path / "unknown-field", [item], [(item, "approve", BASE_REVIEWED_AT)])
    _rewrite_source_units(directory, [unknown_field])
    with pytest.raises(SkillApiBlocked):
        _plan(directory)

    wrong_workspace = _source_unit(item)
    wrong_workspace["workspace_id"] = "other_workspace"
    directory, _ = _write_fixture(tmp_path / "workspace", [item], [(item, "approve", BASE_REVIEWED_AT)])
    _rewrite_source_units(directory, [wrong_workspace])
    with pytest.raises(SkillApiBlocked, match="workspace"):
        _plan(directory)

    directory, _ = _write_fixture(tmp_path / "missing", [item], [(item, "approve", BASE_REVIEWED_AT)])
    _rewrite_source_units(directory, [])
    with pytest.raises(SkillApiBlocked, match="missing source unit"):
        _plan(directory)

    unit = _source_unit(item)
    directory, _ = _write_fixture(tmp_path / "duplicate", [item], [(item, "approve", BASE_REVIEWED_AT)])
    _rewrite_source_units(directory, [unit, unit])
    with pytest.raises(SkillApiBlocked, match="duplicate source_unit_id"):
        _plan(directory)

    directory, _ = _write_fixture(tmp_path / "artifact-hash", [item], [(item, "approve", BASE_REVIEWED_AT)])
    source_path = directory / "source_units.jsonl"
    source_path.write_text(source_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(SkillApiBlocked, match="artifact hash"):
        _plan(directory)


def test_observation_scope_is_compatible_only_under_matching_message_scope(tmp_path: Path) -> None:
    item = _item(key="mwh_observation_scope")
    unit = _source_unit(item)
    item["source_scope"] = {
        "scope_type": "conversation_observation",
        "ref": "observation_scope_1",
        "start": 0,
        "end": len(item["content"]),
        "locator_json": {"observation_id": "observation_scope_1", "message_id": unit["message_id"]},
    }
    directory, _ = _write_fixture(tmp_path / "valid", [item], [(item, "approve", BASE_REVIEWED_AT)])
    _rewrite_source_units(directory, [unit])
    plan = _plan(directory)
    assert plan.counts.writes == 1

    wrong_message_item = _item(key="mwh_observation_wrong_message")
    wrong_message_unit = _source_unit(wrong_message_item)
    wrong_message_item["source_scope"] = {
        "scope_type": "conversation_observation",
        "ref": "observation_scope_2",
        "start": 0,
        "end": len(wrong_message_item["content"]),
        "locator_json": {"observation_id": "observation_scope_2", "message_id": "different-message"},
    }
    wrong_message_item["evidences"][0]["locator_json"]["message_id"] = "different-message"
    directory, _ = _write_fixture(
        tmp_path / "wrong-message",
        [wrong_message_item],
        [(wrong_message_item, "approve", BASE_REVIEWED_AT)],
    )
    _rewrite_source_units(directory, [wrong_message_unit])
    with pytest.raises(SkillApiBlocked, match="message_id"):
        _plan(directory)

    wrong_conversation_item = _item(key="mwh_observation_wrong_conversation")
    wrong_conversation_unit = _source_unit(wrong_conversation_item)
    wrong_conversation_item["source_scope"] = {
        "scope_type": "conversation_observation",
        "ref": "observation_scope_3",
        "start": 0,
        "end": len(wrong_conversation_item["content"]),
        "locator_json": {"observation_id": "observation_scope_3", "message_id": wrong_conversation_unit["message_id"]},
    }
    wrong_conversation_unit["conversation_id"] = "different-conversation"
    directory, _ = _write_fixture(
        tmp_path / "wrong-conversation",
        [wrong_conversation_item],
        [(wrong_conversation_item, "approve", BASE_REVIEWED_AT)],
    )
    _rewrite_source_units(directory, [wrong_conversation_unit])
    with pytest.raises(SkillApiBlocked, match="conversation_id"):
        _plan(directory)

    wrong_scope_item = _item(key="mwh_observation_wrong_scope")
    wrong_scope_unit = _source_unit(wrong_scope_item)
    wrong_scope_item["source_scope"] = {
        "scope_type": "conversation_thread",
        "ref": "thread_scope_1",
        "start": 0,
        "end": len(wrong_scope_item["content"]),
        "locator_json": {"thread_id": "thread_scope_1", "message_id": wrong_scope_unit["message_id"]},
    }
    directory, _ = _write_fixture(
        tmp_path / "wrong-scope",
        [wrong_scope_item],
        [(wrong_scope_item, "approve", BASE_REVIEWED_AT)],
    )
    _rewrite_source_units(directory, [wrong_scope_unit])
    with pytest.raises(SkillApiBlocked, match="scope"):
        _plan(directory)


def test_source_unit_identity_metadata_can_repeat_only_when_it_matches(tmp_path: Path) -> None:
    item = _item()
    matching = _source_unit(item)
    matching["metadata"].update({
        "conversation_id": matching["conversation_id"],
        "message_id": matching["message_id"],
        "role": matching["role"],
        "sequence": matching["sequence"],
        "timestamp": matching["timestamp"],
    })
    directory, _ = _write_fixture(tmp_path / "matching", [item], [(item, "approve", BASE_REVIEWED_AT)])
    _rewrite_source_units(directory, [matching])
    plan = _plan(directory)
    assert plan.source_units[0].metadata["message_id"] == matching["message_id"]
    assert plan.source_units[0].metadata["timestamp"] == matching["timestamp"]

    parented = _source_unit(item)
    parented["parent_unit_id"] = "parent_source_unit_1"
    parented["metadata"]["parent_unit_id"] = "parent_source_unit_1"
    directory, _ = _write_fixture(tmp_path / "parented", [item], [(item, "approve", BASE_REVIEWED_AT)])
    _rewrite_source_units(directory, [parented])
    parent_plan = _plan(directory)
    assert parent_plan.source_units[0].parent_unit_id == "parent_source_unit_1"
    assert parent_plan.source_units[0].metadata["parent_unit_id"] == "parent_source_unit_1"

    invalid_parent = _source_unit(item)
    invalid_parent["parent_unit_id"] = "   "
    directory, _ = _write_fixture(tmp_path / "invalid-parent", [item], [(item, "approve", BASE_REVIEWED_AT)])
    _rewrite_source_units(directory, [invalid_parent])
    with pytest.raises(SkillApiBlocked):
        _plan(directory)

    conflicting = _source_unit(item)
    conflicting["metadata"].update({
        "conversation_id": conflicting["conversation_id"],
        "message_id": "different-message",
        "role": conflicting["role"],
        "sequence": conflicting["sequence"],
        "timestamp": conflicting["timestamp"],
    })
    directory, _ = _write_fixture(tmp_path / "conflicting", [item], [(item, "approve", BASE_REVIEWED_AT)])
    _rewrite_source_units(directory, [conflicting])
    with pytest.raises(SkillApiBlocked):
        _plan(directory)

    for reserved_field in (
        "workspace_id",
        "project_id",
        "source_id",
        "source_unit_hash",
        "source_type",
        "source_path",
        "source_sha256",
        "visibility",
        "sensitivity",
    ):
        unsafe = _source_unit(item)
        unsafe["metadata"][reserved_field] = "unsafe-override"
        directory, _ = _write_fixture(tmp_path / reserved_field, [item], [(item, "approve", BASE_REVIEWED_AT)])
        _rewrite_source_units(directory, [unsafe])
        with pytest.raises(SkillApiBlocked):
            _plan(directory)


def test_cross_handoff_and_unknown_written_map_keys_are_blocked(tmp_path: Path) -> None:
    item = _item()
    directory, _ = _write_fixture(tmp_path, [item], [(item, "approve", BASE_REVIEWED_AT)])
    with pytest.raises(SkillApiBlocked, match="unknown handoff item"):
        _plan(directory, {"other_key": {"memory_id": "mem_other", "version": 1}})

    # A rewritten ledger record with another handoff id is rejected even if its item hash is valid.
    wrong = _approval(item, "approve", BASE_REVIEWED_AT, handoff_id="other_handoff")
    ledger_path = directory / "approval_ledger.jsonl"
    ledger_path.write_text(json.dumps(wrong, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    approval_path = directory / "approval_manifest.json"
    approval = json.loads(approval_path.read_text(encoding="utf-8"))
    approval["ledger_sha256"] = _sha(ledger_path.read_bytes())
    approval_path.write_text(json.dumps(approval, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    handoff_path = directory / "handoff_manifest.json"
    manifest = json.loads(handoff_path.read_text(encoding="utf-8"))
    manifest["approval_manifest_sha256"] = _sha(approval_path.read_bytes())
    handoff_path.write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(SkillApiBlocked, match="different handoff"):
        _plan(directory)


def test_unknown_fields_and_scope_mismatch_are_blocked(tmp_path: Path) -> None:
    item = _item(extra={"unexpected_field": "not allowed"})
    directory, _ = _write_fixture(tmp_path / "unknown", [item], [(item, "approve", BASE_REVIEWED_AT)])
    with pytest.raises(SkillApiBlocked):
        _plan(directory)

    other_conversation = _item(conversation_id="other_conversation")
    directory, _ = _write_fixture(
        tmp_path / "conversation-mismatch",
        [other_conversation],
        [(other_conversation, "approve", BASE_REVIEWED_AT)],
        conversation_ids=[CONVERSATION_ID],
    )
    with pytest.raises(SkillApiBlocked, match="conversation_id"):
        _plan(directory)


def test_plan_is_read_only_and_deterministic(tmp_path: Path) -> None:
    item = _item()
    directory, _ = _write_fixture(tmp_path, [item], [(item, "approve", BASE_REVIEWED_AT)])
    before = {path.name: (path.stat().st_mtime_ns, _sha(path.read_bytes())) for path in directory.iterdir() if path.is_file()}
    first = _plan(directory)
    second = _plan(directory)
    after = {path.name: (path.stat().st_mtime_ns, _sha(path.read_bytes())) for path in directory.iterdir() if path.is_file()}
    assert first.model_dump(mode="json") == second.model_dump(mode="json")
    assert first.plan_id == second.plan_id
    assert before == after


def test_plan_has_no_conversation_cleaner_import() -> None:
    path = Path(__file__).resolve().parents[1] / "src" / "memory_core" / "skill_api" / "handoff.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.append(node.module)
    assert all(not module.startswith("conversation_cleaner") for module in modules)


def test_handoff_artifacts_block_secret_patterns_without_echo(tmp_path: Path) -> None:
    item = _item(key="mwh_secret_scan")
    directory, _ = _write_fixture(tmp_path, [item], [(item, "approve", BASE_REVIEWED_AT)])
    secret = "sk-abcdefghijklmnop1234567890"
    review = directory / "HANDOFF_REVIEW.md"
    review.write_text(review.read_text(encoding="utf-8") + f"\nLeaked key: {secret}\n", encoding="utf-8")
    with pytest.raises(SkillApiBlocked, match="secret scan") as exc_info:
        _plan(directory)
    assert secret not in str(exc_info.value)


def test_symlink_handoff_directory_is_rejected(tmp_path: Path, monkeypatch) -> None:
    item = _item()
    directory, _ = _write_fixture(tmp_path, [item], [(item, "approve", BASE_REVIEWED_AT)])
    link = tmp_path / "handoff-link"
    try:
        os.symlink(directory, link, target_is_directory=True)
    except OSError:
        real_is_symlink = Path.is_symlink
        monkeypatch.setattr(
            Path,
            "is_symlink",
            lambda self: self.resolve() == directory.resolve() or real_is_symlink(self),
        )
        link = directory
    with pytest.raises(SkillApiBlocked, match="symlink"):
        _plan(link)


def test_latest_defer_overrides_earlier_approve_and_blocks_write(tmp_path: Path) -> None:
    item = _item(key="mwh_latest_defer")
    directory, _ = _write_fixture(
        tmp_path,
        [item],
        [
            (item, "approve", "2026-11-22T09:00:00+08:00"),
            (item, "defer", "2026-11-22T10:00:00+08:00"),
        ],
    )
    plan = _plan(directory)
    assert plan.counts.writes == 0
    assert plan.counts.blocked_items == 1
    assert plan.blocked_items[0].decision == "defer"
