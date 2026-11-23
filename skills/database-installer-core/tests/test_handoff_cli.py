from __future__ import annotations

import ast
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path

import db_skill
from memory_core.db import SQLiteStore
from memory_core.profiles import ProfileRegistry

HANDOFF_ID = "handoff_database_skill_cli_test"
WORKSPACE_ID = "ws_cli_handoff"
PROJECT_ID = "project_cli_handoff"
DOMAIN_PROFILE = "generic"
PROFILE_VERSION = "1.0.0"
SOURCE_ID = "source_cli_handoff"
CONVERSATION_ID = "conv_cli_handoff"
OWNER_ID = "owner_cli_handoff"


def _canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha(value: bytes | str) -> str:
    if isinstance(value, str):
        value = value.encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def _json_output(capsys) -> dict:
    return json.loads(capsys.readouterr().out)


def _item(*, key: str = "mwh_cli_1", content: str = "Synthetic approved database handoff fact.", extra: dict | None = None) -> dict:
    value = {
        "idempotency_key": key,
        "operation_id": "op_" + key,
        "workspace_id": WORKSPACE_ID,
        "project_id": PROJECT_ID,
        "task_id": None,
        "conversation_id": CONVERSATION_ID,
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
        "supersedes_ref": None,
        "supersedes_id": None,
        "expected_version": None,
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
        "transition_kind": "new",
    }
    if extra:
        value.update(extra)
    return value


def _source_unit(item: dict, *, sequence: int | None = None) -> dict:
    evidence = item["evidences"][0]
    message_id = evidence["locator_json"].get("message_id") or item["source_scope"]["ref"]
    visibility = "private" if item.get("requested_visibility", "private") == "private" else "workspace"
    return {
        "source_id": item["source_id"],
        "workspace_id": item["workspace_id"],
        "project_id": item["project_id"],
        "source_unit_id": item["source_unit_id"],
        "source_unit_hash": _sha(item["content"]),
        "scope_type": item["source_scope"]["scope_type"],
        "scope_ref": item["source_scope"]["ref"],
        "text_content": item["content"],
        "conversation_id": item.get("conversation_id"),
        "message_id": message_id,
        "role": "user",
        "sequence": sequence if sequence is not None else int(item.get("domain_payload", {}).get("source_sequence", 0)),
        "timestamp": item["observed_at"],
        "source_type": "conversation_handoff_cli_test",
        "source_path": f"memory://{item['source_id']}",
        "source_sha256": "1" * 64,
        "visibility": visibility,
        "sensitivity": item.get("declared_sensitivity", "private"),
        "metadata": {"handoff_id": HANDOFF_ID},
    }


def _approval(item: dict, decision: str, reviewed_at: str) -> dict:
    material = {
        "approval_contract_version": "conversation-memory-handoff-approval-v1",
        "handoff_id": HANDOFF_ID,
        "item_idempotency_key": item["idempotency_key"],
        "item_sha256": _sha(_canonical(item)),
        "decision": decision,
        "reviewer_id": "reviewer-cli-1",
        "reason": f"Controlled {decision} for CLI test.",
        "reviewed_at": reviewed_at,
    }
    return {"approval_id": "hap_" + _sha(_canonical(material))[:32], **material}


def _latest(approvals: list[dict]) -> dict[str, dict]:
    latest: dict[str, dict] = {}
    for approval in approvals:
        key = approval["item_idempotency_key"]
        current = latest.get(key)
        left = (datetime.fromisoformat(approval["reviewed_at"].replace("Z", "+00:00")), approval["approval_id"])
        if current is None:
            latest[key] = approval
            continue
        right = (datetime.fromisoformat(current["reviewed_at"].replace("Z", "+00:00")), current["approval_id"])
        if left > right:
            latest[key] = approval
    return latest


def _write_fixture(tmp_path: Path, items: list[dict], decisions: list[tuple[dict, str, str]]) -> Path:
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
        "HANDOFF_REVIEW.md": "# Handoff Review\n\nSynthetic CLI test artifact.\n",
    }
    for name, value in artifacts.items():
        (directory / name).write_text(value, encoding="utf-8", newline="")

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
    approval_text = json.dumps(approval_manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    (directory / "approval_manifest.json").write_text(approval_text, encoding="utf-8", newline="")

    handoff_manifest = {
        "handoff_contract": "conversation-memory-handoff",
        "handoff_contract_version": "conversation-memory-handoff-v1",
        "handoff_id": HANDOFF_ID,
        "created_at": "2026-11-22T11:00:00+08:00",
        "run_id": "run_handoff_cli_test",
        "plan_id": "plan_handoff_cli_test",
        "source_sha256": "1" * 64,
        "observations_sha256": "2" * 64,
        "workspace_id": WORKSPACE_ID,
        "project_id": PROJECT_ID,
        "domain_profile": DOMAIN_PROFILE,
        "profile_version": PROFILE_VERSION,
        "source_id": SOURCE_ID,
        "conversation_ids": [CONVERSATION_ID],
        "counts": {"write_items": len(items)},
        "artifacts": {name: _sha(value) for name, value in artifacts.items()},
        "approval_manifest_sha256": _sha(approval_text),
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
    return directory


def _profile(tmp_path: Path, *, project_ids: tuple[str, ...] = (PROJECT_ID,)) -> tuple[Path, Path]:
    workspace = tmp_path / "workspace"
    canonical = workspace / "data" / "memory.sqlite3"
    canonical.parent.mkdir(parents=True)
    store = SQLiteStore(canonical)
    store.migrate()
    store.ensure_workspace_project(
        workspace_id=WORKSPACE_ID,
        workspace_name="CLI Handoff Workspace",
        project_id=PROJECT_ID,
        project_name="CLI Handoff Project",
    )
    store.register_profiles(ProfileRegistry())
    profile_path = tmp_path / "profile.json"
    profile = {
        "contract_version": "database-installer-profile-v1",
        "profile_version": "1.0.0",
        "installer_contract_version": "database-installer-core-v1",
        "mode": "real",
        "environment": "development",
        "backend": "sqlite",
        "workspace_root": str(workspace),
        "canonical_db": "data/memory.sqlite3",
        "capabilities": {
            "sqlite": "installed",
            "fts": "available",
            "vector": "disabled",
            "embedding": "not_configured",
        },
        "workspace_id": WORKSPACE_ID,
        "project_ids": list(project_ids),
        "domain_profile": DOMAIN_PROFILE,
        "owner_principal_id": OWNER_ID,
        "created_at": "2026-11-22T12:00:00+08:00",
    }
    profile_path.write_text(json.dumps(profile, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return profile_path, canonical


def _plan_args(profile: Path, handoff_dir: Path, written_map: str | Path, *extra: str) -> list[str]:
    return [
        "handoff", "plan",
        "--profile", str(profile),
        "--handoff-dir", str(handoff_dir),
        "--written-memory-map", str(written_map),
        "--source-id", SOURCE_ID,
        *extra,
        "--json",
    ]


def _artifact_hashes(directory: Path) -> dict[str, str]:
    return {path.name: _sha(path.read_bytes()) for path in directory.iterdir() if path.is_file()}


def _reseal_bad_ledger(directory: Path) -> None:
    approval_path = directory / "approval_manifest.json"
    approval = json.loads(approval_path.read_text(encoding="utf-8"))
    approval["ledger_sha256"] = _sha((directory / "approval_ledger.jsonl").read_bytes())
    approval_path.write_text(json.dumps(approval, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    handoff_path = directory / "handoff_manifest.json"
    manifest = json.loads(handoff_path.read_text(encoding="utf-8"))
    manifest["approval_manifest_sha256"] = _sha(approval_path.read_bytes())
    handoff_path.write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def test_vendored_api_version_and_symbols_are_synced() -> None:
    root = Path(__file__).resolve().parents[1]
    init_text = (root / "vendor" / "memory_core" / "skill_api" / "__init__.py").read_text(encoding="utf-8")
    handoff_text = (root / "vendor" / "memory_core" / "skill_api" / "handoff.py").read_text(encoding="utf-8")
    assert re.search(r'^SKILL_API_VERSION\s*=\s*"1\.4\.0"$', init_text, re.MULTILINE)
    assert "def plan_handoff_ingest(" in handoff_text
    assert "def apply_handoff_ingest(" in handoff_text
    assert "plan_handoff_ingest" in init_text
    assert "apply_handoff_ingest" in init_text
    assert "apply_handoff_ingest_local" in init_text
    assert "conversation_cleaner" not in handoff_text


def test_plan_reports_approve_revoke_pending_and_is_read_only(tmp_path: Path, capsys) -> None:
    profile, canonical = _profile(tmp_path)
    items = [_item(key=f"mwh_cli_{index}") for index in range(5)]
    directory = _write_fixture(
        tmp_path,
        items,
        [
            (items[0], "approve", "2026-11-22T10:00:00+08:00"),
            (items[1], "approve", "2026-11-22T09:00:00+08:00"),
            (items[1], "revoke", "2026-11-22T10:00:00+08:00"),
            (items[2], "approve", "2026-11-22T09:00:00+08:00"),
            (items[2], "revoke", "2026-11-22T10:00:00+08:00"),
            (items[3], "reject", "2026-11-22T10:00:00+08:00"),
        ],
    )
    map_path = tmp_path / "written-map.json"
    map_value = {items[2]["idempotency_key"]: {"memory_id": "mem_cli_written_1", "version": 3}}
    map_path.write_text(json.dumps(map_value), encoding="utf-8")
    before_artifacts = _artifact_hashes(directory)
    before_canonical = _sha(canonical.read_bytes())

    code = db_skill.main(_plan_args(profile, directory, map_path, "--conversation-id", CONVERSATION_ID))
    payload = _json_output(capsys)
    assert code == 0
    assert payload["status"] == "planned"
    assert payload["handoff_id"] == HANDOFF_ID
    assert payload["counts"] == {
        "items": 5,
        "writes": 1,
        "deletes": 1,
        "already_applied": 0,
        "blocked_items": 2,
        "pending_post_write_revocations": 1,
    }
    assert payload["written_memory_map_source"] == "file"
    assert payload["apply_available"] is True
    assert payload["apply_status"] == "confirmation_required"
    assert payload["required_confirmation_token"] == f"handoff-apply:{payload['plan_id']}"
    assert _artifact_hashes(directory) == before_artifacts
    assert _sha(canonical.read_bytes()) == before_canonical

    inline = json.dumps(map_value, separators=(",", ":"))
    code = db_skill.main(_plan_args(profile, directory, inline, "--conversation-id", CONVERSATION_ID))
    inline_payload = _json_output(capsys)
    assert code == 0
    assert inline_payload["written_memory_map_source"] == "inline"
    assert inline_payload["plan_id"] == payload["plan_id"]
    assert _artifact_hashes(directory) == before_artifacts
    assert _sha(canonical.read_bytes()) == before_canonical


def test_plan_requires_project_id_when_profile_is_multi_project(tmp_path: Path, capsys) -> None:
    profile, _ = _profile(tmp_path, project_ids=(PROJECT_ID, "project_other"))
    item = _item()
    directory = _write_fixture(tmp_path, [item], [(item, "approve", "2026-11-22T10:00:00+08:00")])
    map_path = tmp_path / "written-map.json"
    map_path.write_text("{}", encoding="utf-8")

    code = db_skill.main(_plan_args(profile, directory, map_path))
    payload = _json_output(capsys)
    assert code == 2
    assert payload["status"] == "blocked"
    assert "project-id" in payload["error"]["message"]

    code = db_skill.main(_plan_args(profile, directory, map_path, "--project-id", "project_not_in_profile"))
    payload = _json_output(capsys)
    assert code == 2
    assert payload["status"] == "blocked"
    assert "not in the connection profile" in payload["error"]["message"]


def test_plan_rejects_tampered_artifacts_unknown_fields_bad_ledger_and_bad_map(tmp_path: Path, capsys) -> None:
    profile, _ = _profile(tmp_path / "profile-root")
    item = _item()
    directory = _write_fixture(tmp_path / "artifact", [item], [(item, "approve", "2026-11-22T10:00:00+08:00")])
    (directory / "memory_write_batch.jsonl").write_text(
        (directory / "memory_write_batch.jsonl").read_text(encoding="utf-8") + "\n",
        encoding="utf-8",
    )
    code = db_skill.main(_plan_args(profile, directory, "{}"))
    assert code == 2
    assert _json_output(capsys)["status"] == "blocked"

    unknown_item = _item(key="mwh_unknown", extra={"unexpected_field": "blocked"})
    directory = _write_fixture(tmp_path / "unknown", [unknown_item], [(unknown_item, "approve", "2026-11-22T10:00:00+08:00")])
    code = db_skill.main(_plan_args(profile, directory, "{}"))
    assert code == 2
    assert _json_output(capsys)["status"] == "blocked"

    directory = _write_fixture(tmp_path / "ledger", [item], [(item, "approve", "2026-11-22T10:00:00+08:00")])
    (directory / "approval_ledger.jsonl").write_text("{}\n", encoding="utf-8")
    _reseal_bad_ledger(directory)
    code = db_skill.main(_plan_args(profile, directory, "{}"))
    assert code == 2
    assert _json_output(capsys)["status"] == "blocked"

    directory = _write_fixture(tmp_path / "map", [item], [(item, "approve", "2026-11-22T10:00:00+08:00")])
    bad_map = json.dumps({item["idempotency_key"]: {"memory_id": "mem_bad", "version": 1, "extra": True}})
    code = db_skill.main(_plan_args(profile, directory, bad_map))
    assert code == 2
    assert _json_output(capsys)["status"] == "blocked"

    code = db_skill.main(_plan_args(profile, directory, "{"))
    assert code == 2
    payload = _json_output(capsys)
    assert payload["status"] == "blocked"
    assert "valid JSON" in payload["error"]["message"]


def test_plan_rejects_missing_traversal_and_symlink_handoff_dir(tmp_path: Path, capsys, monkeypatch) -> None:
    profile, _ = _profile(tmp_path / "profile-root")
    item = _item()
    directory = _write_fixture(tmp_path / "paths", [item], [(item, "approve", "2026-11-22T10:00:00+08:00")])
    map_path = tmp_path / "map.json"
    map_path.write_text("{}", encoding="utf-8")

    code = db_skill.main(_plan_args(profile, tmp_path / "missing-handoff", map_path))
    assert code == 2
    assert _json_output(capsys)["status"] == "blocked"

    traversal = directory / ".." / directory.name
    code = db_skill.main(_plan_args(profile, traversal, map_path))
    assert code == 2
    payload = _json_output(capsys)
    assert payload["status"] == "blocked"
    assert "parent traversal" in payload["error"]["message"]

    target = directory.resolve()
    real_is_symlink = Path.is_symlink
    monkeypatch.setattr(
        Path,
        "is_symlink",
        lambda self: self.resolve(strict=False) == target or real_is_symlink(self),
    )
    code = db_skill.main(_plan_args(profile, directory, map_path))
    assert code == 2
    payload = _json_output(capsys)
    assert payload["status"] == "blocked"
    assert "symlink" in payload["error"]["message"]


def test_apply_is_blocked_without_confirmation_and_does_not_touch_data(tmp_path: Path, capsys) -> None:
    profile, canonical = _profile(tmp_path)
    item = _item()
    directory = _write_fixture(tmp_path, [item], [(item, "approve", "2026-11-22T10:00:00+08:00")])
    before_artifacts = _artifact_hashes(directory)
    before_canonical = _sha(canonical.read_bytes())
    map_path = tmp_path / "map.json"
    map_path.write_text("{}", encoding="utf-8")

    code = db_skill.main([
        "handoff", "apply",
        "--profile", str(profile),
        "--handoff-dir", str(directory),
        "--written-memory-map", str(map_path),
        "--source-id", SOURCE_ID,
        "--confirm", "not-a-real-token",
        "--json",
    ])
    payload = _json_output(capsys)
    assert code == 2
    assert payload["status"] == "blocked"
    assert payload["error"]["code"] == "confirmation_mismatch"
    assert payload["apply_available"] is True
    assert payload["confirmation_matched"] is False
    assert _artifact_hashes(directory) == before_artifacts
    assert _sha(canonical.read_bytes()) == before_canonical


def test_handoff_cli_uses_only_skill_api_boundary() -> None:
    root = Path(__file__).resolve().parents[1]
    path = root / "scripts" / "db_skill.py"
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text)
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules = [node.module]
        else:
            continue
        for module in modules:
            if module == "memory_core" or module.startswith("memory_core."):
                if module != "memory_core.skill_api":
                    violations.append(f"{path}:{node.lineno}:{module}")
    assert violations == []
    assert "apply_handoff_ingest_local" in text
    assert "conversation_cleaner" not in text
