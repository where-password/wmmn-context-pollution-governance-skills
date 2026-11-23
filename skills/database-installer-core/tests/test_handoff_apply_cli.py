from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import db_skill
from memory_core.db import SQLiteStore
from memory_core.models import MemoryStatus
from memory_core.profiles import ProfileRegistry
from memory_core.skill_api import CapabilitySet, ConnectionProfile

from test_handoff_cli import (
    CONVERSATION_ID,
    DOMAIN_PROFILE,
    HANDOFF_ID,
    OWNER_ID,
    PROFILE_VERSION,
    PROJECT_ID,
    SOURCE_ID,
    WORKSPACE_ID,
    _item as _base_item,
    _reseal_bad_ledger,
    _write_fixture as _write_cli_fixture,
)

PRINCIPAL_ID = OWNER_ID
BASE_REVIEWED_AT = "2026-11-22T10:00:00+08:00"


def _sha(value: bytes | str) -> str:
    if isinstance(value, str):
        value = value.encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def _json_output(capsys) -> dict:
    return json.loads(capsys.readouterr().out)


def _item(
    *,
    key: str = "mwh_cli_1",
    content: str = "Synthetic approved database handoff fact.",
    supersedes_ref: str | None = None,
    extra: dict | None = None,
) -> dict:
    value = _base_item(key=key, content=content, extra=extra)
    if supersedes_ref:
        value["supersedes_ref"] = supersedes_ref
        value["transition_kind"] = "supersedes"
    return value


def _write_fixture(tmp_path: Path, items: list[dict], decisions: list[tuple[dict, str, str]]) -> tuple[Path, dict]:
    directory = _write_cli_fixture(tmp_path, items, decisions)
    manifest = json.loads((directory / "handoff_manifest.json").read_text(encoding="utf-8"))
    return directory, manifest


def _apply_profile(
    tmp_path: Path,
    items: list[dict],
    *,
    domain_profile: str = DOMAIN_PROFILE,
    owner_principal_id: str = PRINCIPAL_ID,
    project_ids: tuple[str, ...] = (PROJECT_ID,),
) -> tuple[Path, Path, SQLiteStore]:
    workspace = tmp_path / "workspace"
    canonical_db = workspace / "data" / "memory.sqlite3"
    store = SQLiteStore(canonical_db)
    store.migrate()
    store.ensure_workspace_project(
        workspace_id=WORKSPACE_ID,
        workspace_name="CLI Handoff Workspace",
        project_id=PROJECT_ID,
        project_name="CLI Handoff Project",
    )
    store.register_profiles(ProfileRegistry())

    profile = ConnectionProfile(
        mode="real",
        environment="development",
        workspace_root=workspace,
        canonical_db="data/memory.sqlite3",
        capabilities=CapabilitySet(sqlite="installed", fts="available"),
        workspace_id=WORKSPACE_ID,
        project_ids=project_ids,
        domain_profile=domain_profile,
        owner_principal_id=owner_principal_id,
        created_at=datetime.now(timezone.utc),
    )
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(profile.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return profile_path, canonical_db, store


def _write_map(tmp_path: Path, value: dict | None = None) -> Path:
    path = tmp_path / "written-memory-map.json"
    path.write_text(json.dumps(value or {}), encoding="utf-8")
    return path


def _plan(profile: Path, directory: Path, map_path: Path | None, *extra: str, capsys) -> tuple[int, dict]:
    args = [
        "handoff", "plan",
        "--profile", str(profile),
        "--handoff-dir", str(directory),
        "--source-id", SOURCE_ID,
        *extra,
    ]
    if map_path is not None:
        args.extend(["--written-memory-map", str(map_path)])
    args.append("--json")
    code = db_skill.main(args)
    return code, _json_output(capsys)


def _apply(profile: Path, directory: Path, map_path: Path | None, confirm: str | None, *extra: str, capsys) -> tuple[int, dict, str]:
    args = [
        "handoff", "apply",
        "--profile", str(profile),
        "--handoff-dir", str(directory),
        "--source-id", SOURCE_ID,
        *extra,
    ]
    if map_path is not None:
        args.extend(["--written-memory-map", str(map_path)])
    if confirm is not None:
        args.extend(["--confirm", confirm])
    args.append("--json")
    code = db_skill.main(args)
    raw = capsys.readouterr().out
    return code, json.loads(raw), raw


def _source_counts(store: SQLiteStore) -> tuple[int, int]:
    with store.read() as connection:
        sources = connection.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
        source_units = connection.execute("SELECT COUNT(*) FROM source_units").fetchone()[0]
    return sources, source_units


def _counts(store: SQLiteStore) -> tuple[int, int]:
    with store.read() as connection:
        memory_count = connection.execute("SELECT COUNT(*) FROM memory_items").fetchone()[0]
        grant_count = connection.execute("SELECT COUNT(*) FROM access_grants").fetchone()[0]
    return memory_count, grant_count


def test_vendored_local_apply_api_version_and_exports_are_synced() -> None:
    root = Path(__file__).resolve().parents[1]
    init_text = (root / "vendor" / "memory_core" / "skill_api" / "__init__.py").read_text(encoding="utf-8")
    handoff_text = (root / "vendor" / "memory_core" / "skill_api" / "handoff.py").read_text(encoding="utf-8")
    assert re.search(r'^SKILL_API_VERSION\s*=\s*"1\.4\.0"$', init_text, re.MULTILINE)
    assert "apply_handoff_ingest_local" in init_text
    assert 'apply_handoff_ingest_local' in init_text[init_text.find('__all__'):]
    assert "def apply_handoff_ingest_local(" in handoff_text


def test_empty_db_plan_confirm_apply_registers_sources_and_is_idempotent(tmp_path: Path, capsys) -> None:
    item = _item(key="mwh_cli_empty_db_sources")
    directory, _ = _write_fixture(tmp_path, [item], [(item, "approve", BASE_REVIEWED_AT)])
    profile, _, store = _apply_profile(tmp_path, [])
    map_path = _write_map(tmp_path)
    assert _source_counts(store) == (0, 0)

    plan_code, plan = _plan(profile, directory, None, capsys=capsys)
    assert plan_code == 0
    assert plan["written_memory_map_source"] == "sqlite"
    code, first, _ = _apply(profile, directory, None, plan["required_confirmation_token"], capsys=capsys)
    assert code == 0
    second_plan_code, second_plan = _plan(profile, directory, None, capsys=capsys)
    assert second_plan_code == 0
    assert second_plan["written_memory_map_source"] == "sqlite"
    assert second_plan["counts"]["writes"] == 0
    assert second_plan["counts"]["already_applied"] == 1
    recurrence, second, _ = _apply(profile, directory, None, second_plan["required_confirmation_token"], capsys=capsys)
    assert recurrence == 0
    assert second["counts"]["writes_succeeded"] == 0
    assert second["counts"]["already_applied"] == 1
    assert _counts(store)[0] == 1
    assert _source_counts(store) == (1, 1)
    assert first["write_results"][0]["memory_id"] == second["already_applied"][0]["memory_id"]


def test_confirmed_apply_writes_active_memory_with_local_grant(tmp_path: Path, capsys) -> None:
    item = _item(key="mwh_cli_apply_success")
    directory, _ = _write_fixture(tmp_path, [item], [(item, "approve", BASE_REVIEWED_AT)])
    profile, _, store = _apply_profile(tmp_path, [item])
    map_path = _write_map(tmp_path)

    plan_code, plan = _plan(profile, directory, map_path, "--conversation-id", CONVERSATION_ID, capsys=capsys)
    assert plan_code == 0
    assert plan["apply_available"] is True
    token = plan["required_confirmation_token"]
    assert token == f"handoff-apply:{plan['plan_id']}"

    code, payload, raw = _apply(profile, directory, map_path, token, "--conversation-id", CONVERSATION_ID, capsys=capsys)
    assert code == 0
    assert payload["command"] == "handoff apply"
    assert payload["status"] == "completed"
    assert payload["confirmation_matched"] is True
    assert payload["counts"]["writes_succeeded"] == 1
    assert payload["authorization"]["provider"] == "local_ephemeral"
    assert payload["authorization"]["actions"] == ["write", "delete"]
    assert payload["authorization"]["revoked_after_use"] is True
    assert "required_confirmation_token" not in payload
    lowered = raw.lower()
    for fragment in ("signature", "hmac", "jti", '"token"', "api_key", "password"):
        assert fragment not in lowered

    memory_id = payload["write_results"][0]["memory_id"]
    record = store.get_memory_record(memory_id)
    assert record is not None
    assert record.status == MemoryStatus.ACTIVE
    assert record.evidences and record.evidences[0].status.value == "verified"
    assert record.domain_payload["conversation_handoff"]["handoff_id"] == HANDOFF_ID
    grant = store.get_grant(payload["authorization"]["grant_id"])
    assert grant is not None
    assert grant.actions == ["write", "delete"]
    assert grant.revoked is True
    assert _source_counts(store) == (1, 1)
    with store.read() as connection:
        source = connection.execute("SELECT * FROM sources WHERE source_id=?", (SOURCE_ID,)).fetchone()
        unit = connection.execute("SELECT * FROM source_units WHERE source_unit_id=?", (item["source_unit_id"],)).fetchone()
    assert source is not None
    assert source["visibility"] == "private"
    assert source["owner_principal_id"] == PRINCIPAL_ID
    assert unit is not None
    assert unit["text_content"] == item["content"]


def test_missing_wrong_and_stale_confirmation_are_blocked_without_writes(tmp_path: Path, capsys) -> None:
    item = _item(key="mwh_cli_confirm")
    directory, _ = _write_fixture(tmp_path, [item], [(item, "approve", BASE_REVIEWED_AT)])
    profile, _, store = _apply_profile(tmp_path, [item])
    map_path = _write_map(tmp_path)
    _, plan = _plan(profile, directory, map_path, capsys=capsys)

    code, payload, _ = _apply(profile, directory, map_path, None, capsys=capsys)
    assert code == 2
    assert payload["error"]["code"] == "confirmation_required"
    assert _counts(store) == (0, 0)

    code, payload, _ = _apply(profile, directory, map_path, "not-the-plan-token", capsys=capsys)
    assert code == 2
    assert payload["error"]["code"] == "confirmation_mismatch"
    assert _counts(store) == (0, 0)

    code, applied, _ = _apply(profile, directory, map_path, plan["required_confirmation_token"], capsys=capsys)
    assert code == 0
    memory_id = applied["write_results"][0]["memory_id"]
    version = applied["write_results"][0]["version"]
    changed_map = _write_map(tmp_path, {item["idempotency_key"]: {"memory_id": memory_id, "version": version}})
    _, changed_plan = _plan(profile, directory, changed_map, capsys=capsys)
    assert changed_plan["plan_id"] != plan["plan_id"]

    before = _counts(store)
    code, payload, _ = _apply(profile, directory, changed_map, plan["required_confirmation_token"], capsys=capsys)
    assert code == 2
    assert payload["error"]["code"] == "confirmation_mismatch"
    assert _counts(store) == before


def test_repeated_apply_is_idempotent_and_uses_new_revoked_grants(tmp_path: Path, capsys) -> None:
    item = _item(key="mwh_cli_repeat")
    directory, _ = _write_fixture(tmp_path, [item], [(item, "approve", BASE_REVIEWED_AT)])
    profile, _, store = _apply_profile(tmp_path, [item])
    _, first_plan = _plan(profile, directory, None, capsys=capsys)
    first_code, first, _ = _apply(profile, directory, None, first_plan["required_confirmation_token"], capsys=capsys)
    assert first_code == 0
    _, second_plan = _plan(profile, directory, None, capsys=capsys)
    assert second_plan["counts"]["already_applied"] == 1
    second_code, second, _ = _apply(profile, directory, None, second_plan["required_confirmation_token"], capsys=capsys)
    assert second_code == 0
    assert second["counts"]["writes_succeeded"] == 0
    assert first["write_results"][0]["memory_id"] == second["already_applied"][0]["memory_id"]
    assert first["authorization"]["grant_id"] != second["authorization"]["grant_id"]
    assert _counts(store)[0] == 1
    assert _source_counts(store) == (1, 1)
    assert store.get_grant(first["authorization"]["grant_id"]).revoked is True
    assert store.get_grant(second["authorization"]["grant_id"]).revoked is True


def test_confirmed_apply_supersedes_old_memory(tmp_path: Path, capsys) -> None:
    old = _item(key="mwh_cli_old")
    new = _item(key="mwh_cli_new", content="Updated confirmed CLI handoff fact.", supersedes_ref=old["idempotency_key"])
    profile, _, store = _apply_profile(tmp_path, [old, new])
    first_directory, _ = _write_fixture(tmp_path / "first", [old], [(old, "approve", BASE_REVIEWED_AT)])
    _, plan = _plan(profile, first_directory, None, capsys=capsys)
    _, first, _ = _apply(profile, first_directory, None, plan["required_confirmation_token"], capsys=capsys)
    old_memory_id = first["write_results"][0]["memory_id"]
    old_version = first["write_results"][0]["version"]

    combined, _ = _write_fixture(
        tmp_path / "combined",
        [old, new],
        [(old, "approve", BASE_REVIEWED_AT), (new, "approve", "2026-11-22T11:00:00+08:00")],
    )
    _, plan = _plan(profile, combined, None, capsys=capsys)
    assert plan["written_memory_map_source"] == "sqlite"
    assert plan["counts"]["already_applied"] == 1
    code, payload, _ = _apply(profile, combined, None, plan["required_confirmation_token"], capsys=capsys)
    assert code == 0
    new_record = store.get_memory_record(payload["write_results"][0]["memory_id"])
    old_record = store.get_memory_record(old_memory_id)
    assert old_record.status == MemoryStatus.SUPERSEDED
    assert new_record.supersedes_id == old_memory_id
    assert new_record.version == old_version + 1


def test_auto_map_conflict_with_explicit_override_is_blocked(tmp_path: Path, capsys) -> None:
    old = _item(key="mwh_cli_auto_conflict_old")
    new = _item(
        key="mwh_cli_auto_conflict_new",
        content="Conflicting automatic map override must not write.",
        supersedes_ref=old["idempotency_key"],
    )
    profile, _, store = _apply_profile(tmp_path, [old, new])
    first_directory, _ = _write_fixture(tmp_path / "first", [old], [(old, "approve", BASE_REVIEWED_AT)])
    _, first_plan = _plan(profile, first_directory, None, capsys=capsys)
    _, first, _ = _apply(profile, first_directory, None, first_plan["required_confirmation_token"], capsys=capsys)
    old_memory_id = first["write_results"][0]["memory_id"]

    combined, _ = _write_fixture(
        tmp_path / "combined",
        [old, new],
        [(old, "approve", BASE_REVIEWED_AT), (new, "approve", "2026-11-22T11:00:00+08:00")],
    )
    conflict_map = tmp_path / "conflicting-written-map.json"
    conflict_map.write_text(
        json.dumps({
            old["idempotency_key"]: {"memory_id": "mem_wrong_override", "version": 1},
        }),
        encoding="utf-8",
    )

    code, payload = _plan(profile, combined, conflict_map, capsys=capsys)
    assert code == 2
    assert payload["status"] == "blocked"
    assert "conflicts with the canonical idempotency mapping" in payload["error"]["message"]
    assert _counts(store)[0] == 1
    assert store.get_memory_record(old_memory_id).status == MemoryStatus.ACTIVE


def test_idempotency_key_reuse_with_different_request_is_blocked(tmp_path: Path, capsys) -> None:
    original = _item(key="mwh_cli_reused_key", content="Original approved request.")
    profile, _, store = _apply_profile(tmp_path, [original])
    original_directory, _ = _write_fixture(
        tmp_path / "original",
        [original],
        [(original, "approve", BASE_REVIEWED_AT)],
    )
    _, plan = _plan(profile, original_directory, None, capsys=capsys)
    _, applied, _ = _apply(profile, original_directory, None, plan["required_confirmation_token"], capsys=capsys)
    memory_id = applied["write_results"][0]["memory_id"]

    changed = _item(key=original["idempotency_key"], content="Different request reusing the same key.")
    changed_directory, _ = _write_fixture(
        tmp_path / "changed",
        [changed],
        [(changed, "approve", "2026-11-22T11:00:00+08:00")],
    )
    code, payload = _plan(profile, changed_directory, None, capsys=capsys)
    assert code == 2
    assert payload["status"] == "blocked"
    assert "idempotency key was reused with a different request" in payload["error"]["message"]
    assert _counts(store)[0] == 1
    record = store.get_memory_record(memory_id)
    assert record is not None
    assert record.content == "Original approved request."


def test_supersedes_without_canonical_mapping_is_blocked(tmp_path: Path, capsys) -> None:
    old = _item(key="mwh_cli_unwritten_old")
    new = _item(
        key="mwh_cli_unwritten_new",
        content="A supersede that lacks a canonical target must not write.",
        supersedes_ref=old["idempotency_key"],
    )
    profile, _, store = _apply_profile(tmp_path, [old, new])
    directory, _ = _write_fixture(
        tmp_path,
        [old, new],
        [(old, "approve", BASE_REVIEWED_AT), (new, "approve", "2026-11-22T11:00:00+08:00")],
    )

    code, payload = _plan(profile, directory, None, capsys=capsys)
    assert code == 2
    assert payload["status"] == "blocked"
    assert "canonical or explicit written_memory_map mapping" in payload["error"]["message"]
    assert _counts(store) == (0, 0)


def test_confirmed_apply_revoke_soft_deletes_memory(tmp_path: Path, capsys) -> None:
    item = _item(key="mwh_cli_revoke")
    profile, _, store = _apply_profile(tmp_path, [item])
    approve_directory, _ = _write_fixture(tmp_path / "approve", [item], [(item, "approve", BASE_REVIEWED_AT)])
    _, plan = _plan(profile, approve_directory, None, capsys=capsys)
    _, written, _ = _apply(profile, approve_directory, None, plan["required_confirmation_token"], capsys=capsys)
    memory_id = written["write_results"][0]["memory_id"]

    revoke_directory, _ = _write_fixture(
        tmp_path / "revoke",
        [item],
        [(item, "approve", "2026-11-22T09:00:00+08:00"), (item, "revoke", "2026-11-22T10:00:00+08:00")],
    )
    _, plan = _plan(profile, revoke_directory, None, capsys=capsys)
    assert plan["written_memory_map_source"] == "sqlite"
    code, payload, _ = _apply(profile, revoke_directory, None, plan["required_confirmation_token"], capsys=capsys)
    assert code == 0
    record = store.get_memory_record(memory_id)
    assert payload["counts"]["deletes_succeeded"] == 1
    assert payload["delete_results"][0]["memory_ids"] == [memory_id]
    assert record is not None and record.deleted_at is not None


def test_profile_project_domain_and_principal_mismatches_are_blocked(tmp_path: Path, capsys) -> None:
    item = _item(key="mwh_cli_scope_mismatch")
    directory, _ = _write_fixture(tmp_path, [item], [(item, "approve", BASE_REVIEWED_AT)])
    profile, _, store = _apply_profile(tmp_path, [item])
    map_path = _write_map(tmp_path)
    _, plan = _plan(profile, directory, map_path, capsys=capsys)

    code, payload, _ = _apply(
        profile, directory, map_path, plan["required_confirmation_token"],
        "--principal-id", "another-principal", capsys=capsys,
    )
    assert code == 2
    assert payload["status"] == "blocked"
    assert "principal_id" in payload["error"]["message"]

    code, payload, _ = _apply(
        profile, directory, map_path, plan["required_confirmation_token"],
        "--ttl-seconds", "0", capsys=capsys,
    )
    assert code == 2
    assert "ttl_seconds" in payload["error"]["message"]

    code, payload, _ = _apply(
        profile, directory, map_path, plan["required_confirmation_token"],
        "--project-id", "project-outside", capsys=capsys,
    )
    assert code == 2
    assert "project_id" in payload["error"]["message"]
    assert _counts(store) == (0, 0)

    bad_domain_profile, _, bad_domain_store = _apply_profile(tmp_path / "domain", [item], domain_profile="novel")
    code, payload = _plan(bad_domain_profile, directory, map_path, capsys=capsys)
    assert code == 2
    assert payload["status"] == "blocked"
    assert _counts(bad_domain_store) == (0, 0)


def test_bad_handoff_hash_ledger_map_and_paths_are_blocked(tmp_path: Path, capsys, monkeypatch) -> None:
    item = _item(key="mwh_cli_bad_handoff")
    profile, _, store = _apply_profile(tmp_path, [item])
    map_path = _write_map(tmp_path)

    tampered, _ = _write_fixture(tmp_path / "tampered", [item], [(item, "approve", BASE_REVIEWED_AT)])
    artifact = tampered / "memory_write_batch.jsonl"
    artifact.write_text(artifact.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    _, plan = _plan(profile, tampered, map_path, capsys=capsys)
    assert plan["status"] == "blocked"

    bad_ledger, _ = _write_fixture(tmp_path / "bad-ledger", [item], [(item, "approve", BASE_REVIEWED_AT)])
    (bad_ledger / "approval_ledger.jsonl").write_text("{}\n", encoding="utf-8")
    _reseal_bad_ledger(bad_ledger)
    _, plan = _plan(profile, bad_ledger, map_path, capsys=capsys)
    assert plan["status"] == "blocked"

    valid_directory, _ = _write_fixture(tmp_path / "valid", [item], [(item, "approve", BASE_REVIEWED_AT)])
    bad_map = tmp_path / "bad-map.json"
    bad_map.write_text(json.dumps({item["idempotency_key"]: {"memory_id": "mem_bad", "version": 1, "extra": True}}), encoding="utf-8")
    code, payload, _ = _apply(profile, valid_directory, bad_map, "anything", capsys=capsys)
    assert code == 2
    assert payload["status"] == "blocked"

    code, payload, _ = _apply(profile, tmp_path / "missing", map_path, "anything", capsys=capsys)
    assert code == 2
    assert payload["status"] == "blocked"

    target = valid_directory.resolve()
    real_is_symlink = Path.is_symlink
    monkeypatch.setattr(
        Path,
        "is_symlink",
        lambda self: self.resolve(strict=False) == target or real_is_symlink(self),
    )
    code, payload, _ = _apply(profile, valid_directory, map_path, "anything", capsys=capsys)
    assert code == 2
    assert payload["status"] == "blocked"
    assert "symlink" in payload["error"]["message"]
    assert _counts(store) == (0, 0)


def test_apply_item_verification_failure_returns_exit_four(tmp_path: Path, capsys) -> None:
    item = _item(key="mwh_cli_item_failure")
    item["supersedes_id"] = "mem_missing_cli_target"
    directory, _ = _write_fixture(tmp_path, [item], [(item, "approve", BASE_REVIEWED_AT)])
    profile, _, store = _apply_profile(tmp_path, [])
    map_path = _write_map(tmp_path)
    _, plan = _plan(profile, directory, map_path, capsys=capsys)
    code, payload, _ = _apply(profile, directory, map_path, plan["required_confirmation_token"], capsys=capsys)
    assert code == 4
    assert payload["status"] == "stopped"
    assert payload["stopped_on_item"]["idempotency_key"] == item["idempotency_key"]
    assert payload["write_results"] == []
    with store.read() as connection:
        assert connection.execute("SELECT COUNT(*) FROM memory_items").fetchone()[0] == 0
        grant = connection.execute("SELECT revoked FROM access_grants").fetchone()
    assert grant is not None
    assert grant["revoked"] == 1


def test_apply_json_has_stable_fields_without_temporary_secrets(tmp_path: Path, capsys) -> None:
    item = _item(key="mwh_cli_stable_output")
    directory, _ = _write_fixture(tmp_path, [item], [(item, "approve", BASE_REVIEWED_AT)])
    profile, _, _ = _apply_profile(tmp_path, [item])
    map_path = _write_map(tmp_path)
    _, plan = _plan(profile, directory, map_path, capsys=capsys)
    code, payload, raw = _apply(profile, directory, map_path, plan["required_confirmation_token"], capsys=capsys)
    assert code == 0
    assert set(payload) == {
        "command", "status", "handoff_id", "plan_id", "confirmation_matched", "counts",
        "write_results", "delete_results", "already_applied", "blocked_items",
        "pending_post_write_revocations", "before_counts", "after_counts", "stopped_on_item",
        "failure", "applied_before_stop", "authorization", "apply_contract_version",
        "apply_available", "read_only",
    }
    assert payload["apply_available"] is True
    assert payload["read_only"] is False
    assert payload["write_results"]
    assert payload["authorization"]["provider"] == "local_ephemeral"
    lowered = raw.lower()
    for fragment in ("signature", "hmac", "jti", '"token"', "authorization_header", "api_key", "password"):
        assert fragment not in lowered