from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from memory_core.db import SQLiteStore, utc_now_ts
from memory_core.models import MemoryStatus
from memory_core.profiles import ProfileRegistry
from memory_core.skill_api import (
    SKILL_API_VERSION,
    SkillApiBlocked,
    apply_handoff_ingest_local,
    resolve_written_memory_map_from_canonical,
)

from .test_skill_api_handoff import (
    BASE_REVIEWED_AT,
    DOMAIN_PROFILE,
    HANDOFF_ID,
    PROFILE_VERSION,
    PROJECT_ID,
    SOURCE_ID,
    WORKSPACE_ID,
    _item,
    _source_unit,
    _write_fixture,
)

PRINCIPAL_ID = "local-handoff-user"


def _sha(value: bytes | str) -> str:
    if isinstance(value, str):
        value = value.encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def _prepare_database(tmp_path: Path, items: list[dict], *, register_profiles: bool = True) -> tuple[Path, SQLiteStore]:
    db_path = tmp_path / "local-apply.sqlite3"
    store = SQLiteStore(db_path)
    store.migrate()
    store.ensure_workspace_project(
        workspace_id=WORKSPACE_ID,
        workspace_name="Local Handoff Workspace",
        project_id=PROJECT_ID,
        project_name="Local Handoff Project",
    )
    profiles = ProfileRegistry()
    if register_profiles:
        store.register_profiles(profiles)

    return db_path, store


def _apply_local(
    directory: Path,
    db_path: Path,
    *,
    written_memory_map=None,
    principal_id: str | None = PRINCIPAL_ID,
    owner_principal_id: str | None = None,
    ttl_seconds: int = 300,
    domain_profile: str = DOMAIN_PROFILE,
    profile_version: str = PROFILE_VERSION,
):
    return apply_handoff_ingest_local(
        canonical_db=db_path,
        handoff_dir=directory,
        handoff_id=HANDOFF_ID,
        workspace_id=WORKSPACE_ID,
        project_id=PROJECT_ID,
        domain_profile=domain_profile,
        profile_version=profile_version,
        source_id=SOURCE_ID,
        written_memory_map={} if written_memory_map is None else written_memory_map,
        principal_id=principal_id,
        owner_principal_id=owner_principal_id,
        ttl_seconds=ttl_seconds,
    )


def _counts(store: SQLiteStore) -> tuple[int, int]:
    with store.read() as connection:
        grants = connection.execute("SELECT COUNT(*) FROM access_grants").fetchone()[0]
        memories = connection.execute("SELECT COUNT(*) FROM memory_items").fetchone()[0]
    return grants, memories


def _insert_foreign_memory(store: SQLiteStore) -> str:
    memory_id = "mem_foreign_project"
    source_id = "source_foreign_project"
    unit_id = "unit_foreign_project"
    now = utc_now_ts()
    with store.transaction() as connection:
        connection.execute(
            "INSERT OR IGNORE INTO projects(project_id,workspace_id,name,created_at) VALUES(?,?,?,?)",
            ("foreign-project", WORKSPACE_ID, "Foreign Project", now),
        )
        connection.execute(
            """INSERT INTO sources(source_id,workspace_id,project_id,visibility,owner_principal_id,
               source_path,source_sha256,source_type,sensitivity,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (source_id, WORKSPACE_ID, "foreign-project", "workspace", None,
             "memory://foreign", "a" * 64, "foreign_test", "private", now),
        )
        connection.execute(
            """INSERT INTO source_units(source_unit_id,source_id,project_id,parent_unit_id,scope_type,
               scope_ref,source_unit_hash,text_content,metadata_json,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (unit_id, source_id, "foreign-project", None, "foreign", "foreign", "a" * 64,
             "Foreign project memory.", "{}", now),
        )
        connection.execute(
            """INSERT INTO memory_items(memory_id,workspace_id,project_id,owner_principal_id,
               domain_profile,profile_version,memory_type,content,content_hash,status,sensitivity,
               visibility,source_id,source_unit_id,source_scope_json,confidence,version,
               domain_payload_json,idempotency_key,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (memory_id, WORKSPACE_ID, "foreign-project", "other-owner", DOMAIN_PROFILE,
             PROFILE_VERSION, "fact", "Foreign project memory.", "b" * 64, "active", "private",
             "private", source_id, unit_id, "{}", 0.9, 1, "{}", "foreign-idem", now, now),
        )
    return memory_id


def _reseal_bad_ledger(directory: Path) -> None:
    approval_path = directory / "approval_manifest.json"
    approval = json.loads(approval_path.read_text(encoding="utf-8"))
    approval["ledger_sha256"] = _sha((directory / "approval_ledger.jsonl").read_bytes())
    approval_path.write_text(json.dumps(approval, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    handoff_path = directory / "handoff_manifest.json"
    manifest = json.loads(handoff_path.read_text(encoding="utf-8"))
    manifest["approval_manifest_sha256"] = _sha(approval_path.read_bytes())
    handoff_path.write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def test_local_apply_writes_active_memory_and_persists_scoped_grant(tmp_path: Path) -> None:
    item = _item(key="mwh_local_success")
    directory, _ = _write_fixture(tmp_path, [item], [(item, "approve", BASE_REVIEWED_AT)])
    db_path, store = _prepare_database(tmp_path, [item])

    result = _apply_local(directory, db_path, owner_principal_id=PRINCIPAL_ID, principal_id=None)

    assert SKILL_API_VERSION == "1.4.0"
    assert result.status == "completed"
    assert result.contract_version == "skill-handoff-ingest-local-apply-v1"
    assert result.apply_result.counts.writes_succeeded == 1
    assert result.apply_result.production_writes is False
    assert result.apply_result.milvus_writes is False
    assert result.apply_result.alias_changes is False
    assert result.apply_result.real_llm_called is False

    memory_id = result.apply_result.write_results[0].memory_id
    record = store.get_memory_record(memory_id)
    assert record is not None
    assert record.status == MemoryStatus.ACTIVE
    assert record.domain_payload["conversation_handoff"]["handoff_id"] == HANDOFF_ID

    grant = store.get_grant(result.authorization.grant_id)
    assert grant is not None
    assert grant.principal_id == PRINCIPAL_ID
    assert grant.workspace_id == WORKSPACE_ID
    assert grant.scope.project_ids == [PROJECT_ID]
    assert grant.scope.domains == [DOMAIN_PROFILE]
    assert grant.actions == ["write", "delete"]
    assert grant.revoked is True
    assert grant.revoked_reason == "local_handoff_apply_finished"
    assert result.authorization.provider == "local_ephemeral"
    assert result.authorization.actions == ("write", "delete")
    assert result.authorization.revoked_after_use is True

    with store.read() as connection:
        source = connection.execute("SELECT * FROM sources WHERE source_id=?", (SOURCE_ID,)).fetchone()
        unit = connection.execute("SELECT * FROM source_units WHERE source_unit_id=?", (item["source_unit_id"],)).fetchone()
    assert source is not None
    assert source["visibility"] == "private"
    assert source["owner_principal_id"] == PRINCIPAL_ID
    assert unit is not None
    assert unit["text_content"] == item["content"]
    metadata = json.loads(unit["metadata_json"])
    assert metadata["conversation_id"] == item["conversation_id"]
    assert metadata["message_id"] == item["evidences"][0]["locator_json"]["message_id"]

    serialized = result.model_dump_json().lower()
    for forbidden_fragment in ("signature", "hmac", "api_key", "password", "authorization_header", "\"token\""):
        assert forbidden_fragment not in serialized


def test_written_map_resolver_reads_canonical_idempotency_records(tmp_path: Path) -> None:
    item = _item(key="mwh_local_resolver")
    directory, _ = _write_fixture(tmp_path, [item], [(item, "approve", BASE_REVIEWED_AT)])
    db_path, _ = _prepare_database(tmp_path, [item])

    before, before_source = resolve_written_memory_map_from_canonical(
        canonical_db=db_path,
        handoff_dir=directory,
        workspace_id=WORKSPACE_ID,
        project_id=PROJECT_ID,
        domain_profile=DOMAIN_PROFILE,
        profile_version=PROFILE_VERSION,
        principal_id=PRINCIPAL_ID,
    )
    assert before == {}
    assert before_source == "sqlite"

    applied = _apply_local(directory, db_path)
    memory_id = applied.apply_result.write_results[0].memory_id
    after, after_source = resolve_written_memory_map_from_canonical(
        canonical_db=db_path,
        handoff_dir=directory,
        workspace_id=WORKSPACE_ID,
        project_id=PROJECT_ID,
        domain_profile=DOMAIN_PROFILE,
        profile_version=PROFILE_VERSION,
        principal_id=PRINCIPAL_ID,
    )
    assert after[item["idempotency_key"]]["memory_id"] == memory_id
    assert after[item["idempotency_key"]]["version"] == 1
    assert len(after[item["idempotency_key"]]["request_sha256"]) == 64
    assert after_source == "sqlite"

    merged, merged_source = resolve_written_memory_map_from_canonical(
        canonical_db=db_path,
        handoff_dir=directory,
        workspace_id=WORKSPACE_ID,
        project_id=PROJECT_ID,
        domain_profile=DOMAIN_PROFILE,
        profile_version=PROFILE_VERSION,
        principal_id=PRINCIPAL_ID,
        explicit_written_memory_map={
            item["idempotency_key"]: {"memory_id": memory_id, "version": 1},
        },
        explicit_source="inline",
    )
    assert merged == after
    assert merged_source == "sqlite+inline"

    with pytest.raises(SkillApiBlocked, match="conflicts with the canonical idempotency mapping"):
        resolve_written_memory_map_from_canonical(
            canonical_db=db_path,
            handoff_dir=directory,
            workspace_id=WORKSPACE_ID,
            project_id=PROJECT_ID,
            domain_profile=DOMAIN_PROFILE,
            profile_version=PROFILE_VERSION,
            principal_id=PRINCIPAL_ID,
            explicit_written_memory_map={
                item["idempotency_key"]: {"memory_id": "mem_conflicting_override", "version": 1},
            },
            explicit_source="inline",
        )


def test_local_apply_repeat_is_idempotent_with_new_revoked_grant(tmp_path: Path) -> None:
    item = _item(key="mwh_local_repeat")
    directory, _ = _write_fixture(tmp_path, [item], [(item, "approve", BASE_REVIEWED_AT)])
    db_path, store = _prepare_database(tmp_path, [item])

    first = _apply_local(directory, db_path)
    second = _apply_local(directory, db_path)

    assert first.status == "completed"
    assert second.status == "completed"
    assert first.apply_result.write_results[0].memory_id == second.apply_result.write_results[0].memory_id
    assert first.authorization.grant_id != second.authorization.grant_id
    with store.read() as connection:
        assert connection.execute("SELECT COUNT(*) FROM memory_items").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM access_grants").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM source_units").fetchone()[0] == 1
    assert store.get_grant(first.authorization.grant_id).revoked is True
    assert store.get_grant(second.authorization.grant_id).revoked is True


def test_local_apply_supersedes_uses_explicit_written_map(tmp_path: Path) -> None:
    old = _item(key="mwh_local_old")
    new = _item(key="mwh_local_new", content="Updated locally authorized handoff fact.", supersedes_ref=old["idempotency_key"])
    db_path, store = _prepare_database(tmp_path, [old, new])

    first_directory, _ = _write_fixture(tmp_path / "first", [old], [(old, "approve", BASE_REVIEWED_AT)])
    first = _apply_local(first_directory, db_path)
    old_memory_id = first.apply_result.write_results[0].memory_id
    old_version = first.apply_result.write_results[0].version

    combined, _ = _write_fixture(
        tmp_path / "combined",
        [old, new],
        [(old, "approve", BASE_REVIEWED_AT), (new, "approve", "2026-11-22T11:00:00+08:00")],
    )
    result = _apply_local(
        combined,
        db_path,
        written_memory_map={old["idempotency_key"]: {"memory_id": old_memory_id, "version": old_version}},
    )

    new_record = store.get_memory_record(result.apply_result.write_results[0].memory_id)
    old_record = store.get_memory_record(old_memory_id)
    assert result.apply_result.counts.writes_succeeded == 1
    assert old_record.status == MemoryStatus.SUPERSEDED
    assert new_record.supersedes_id == old_memory_id
    assert new_record.version == old_version + 1


def test_local_apply_revoke_soft_deletes_row(tmp_path: Path) -> None:
    item = _item(key="mwh_local_revoke")
    db_path, store = _prepare_database(tmp_path, [item])
    approve_directory, _ = _write_fixture(tmp_path / "approve", [item], [(item, "approve", BASE_REVIEWED_AT)])
    written = _apply_local(approve_directory, db_path)
    memory_id = written.apply_result.write_results[0].memory_id

    revoke_directory, _ = _write_fixture(
        tmp_path / "revoke",
        [item],
        [(item, "approve", "2026-11-22T09:00:00+08:00"), (item, "revoke", "2026-11-22T10:00:00+08:00")],
    )
    result = _apply_local(
        revoke_directory,
        db_path,
        written_memory_map={item["idempotency_key"]: {"memory_id": memory_id, "version": 1}},
    )
    record = store.get_memory_record(memory_id)
    assert result.apply_result.counts.deletes_succeeded == 1
    assert result.apply_result.delete_results[0].memory_ids == [memory_id]
    assert record is not None
    assert record.deleted_at is not None


def test_local_apply_rejects_cross_project_delete_before_grant(tmp_path: Path) -> None:
    item = _item(key="mwh_local_cross_scope")
    db_path, store = _prepare_database(tmp_path, [item])
    foreign_memory_id = _insert_foreign_memory(store)
    directory, _ = _write_fixture(
        tmp_path,
        [item],
        [(item, "approve", "2026-11-22T09:00:00+08:00"), (item, "revoke", "2026-11-22T10:00:00+08:00")],
    )
    with pytest.raises(SkillApiBlocked, match="outside the requested workspace/project scope"):
        _apply_local(
            directory,
            db_path,
            written_memory_map={item["idempotency_key"]: {"memory_id": foreign_memory_id, "version": 1}},
        )
    assert _counts(store) == (0, 1)


def test_local_apply_invalid_handoff_blocks_before_grant_or_memory_write(tmp_path: Path) -> None:
    item = _item(key="mwh_local_invalid")
    db_path, store = _prepare_database(tmp_path, [item])

    tampered, _ = _write_fixture(tmp_path / "tampered", [item], [(item, "approve", BASE_REVIEWED_AT)])
    artifact = tampered / "memory_write_batch.jsonl"
    artifact.write_text(artifact.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(SkillApiBlocked, match="artifact hash"):
        _apply_local(tampered, db_path)
    assert _counts(store) == (0, 0)

    bad_ledger, _ = _write_fixture(tmp_path / "bad-ledger", [item], [(item, "approve", BASE_REVIEWED_AT)])
    (bad_ledger / "approval_ledger.jsonl").write_text("{}\n", encoding="utf-8")
    _reseal_bad_ledger(bad_ledger)
    with pytest.raises(SkillApiBlocked):
        _apply_local(bad_ledger, db_path)
    assert _counts(store) == (0, 0)

    unknown_item = _item(key="mwh_local_unknown", extra={"unexpected_field": "blocked"})
    unknown_directory, _ = _write_fixture(
        tmp_path / "unknown",
        [unknown_item],
        [(unknown_item, "approve", BASE_REVIEWED_AT)],
    )
    with pytest.raises(SkillApiBlocked):
        _apply_local(unknown_directory, db_path)
    assert _counts(store) == (0, 0)


def test_local_apply_rejects_invalid_principal_ttl_domain_and_profile(tmp_path: Path) -> None:
    item = _item(key="mwh_local_invalid_inputs")
    directory, _ = _write_fixture(tmp_path, [item], [(item, "approve", BASE_REVIEWED_AT)])
    db_path, store = _prepare_database(tmp_path, [item])

    with pytest.raises(SkillApiBlocked, match="ttl_seconds"):
        _apply_local(directory, db_path, ttl_seconds=0)
    with pytest.raises(SkillApiBlocked, match="ttl_seconds"):
        _apply_local(directory, db_path, ttl_seconds=3601)
    with pytest.raises(SkillApiBlocked, match="principal_id"):
        _apply_local(directory, db_path, principal_id="")
    with pytest.raises(SkillApiBlocked, match="conflict"):
        _apply_local(directory, db_path, principal_id="one", owner_principal_id="two")
    with pytest.raises(SkillApiBlocked):
        _apply_local(directory, db_path, domain_profile="other-domain")
    with pytest.raises(SkillApiBlocked):
        _apply_local(directory, db_path, profile_version="9.9.9")
    assert _counts(store) == (0, 0)


def test_local_apply_source_conflict_revokes_grant_without_memory(tmp_path: Path) -> None:
    item = _item(key="mwh_local_source_conflict")
    directory, _ = _write_fixture(tmp_path, [item], [(item, "approve", BASE_REVIEWED_AT)])
    db_path, store = _prepare_database(tmp_path, [item])
    unit = _source_unit(item)
    store.insert_source(
        source_id=SOURCE_ID,
        workspace_id=WORKSPACE_ID,
        project_id=PROJECT_ID,
        visibility="private",
        owner_principal_id=PRINCIPAL_ID,
        source_path=unit["source_path"],
        source_sha256="2" * 64,
        source_type=unit["source_type"],
        sensitivity=unit["sensitivity"],
    )
    with pytest.raises(SkillApiBlocked, match="existing source"):
        _apply_local(directory, db_path)
    with store.read() as connection:
        assert connection.execute("SELECT COUNT(*) FROM memory_items").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM source_units").fetchone()[0] == 0
        grant = connection.execute("SELECT grant_id,revoked FROM access_grants").fetchone()
    assert grant is not None
    assert grant["revoked"] == 1


def test_local_apply_unit_conflict_revokes_grant_without_memory(tmp_path: Path) -> None:
    item = _item(key="mwh_local_unit_conflict")
    directory, _ = _write_fixture(tmp_path, [item], [(item, "approve", BASE_REVIEWED_AT)])
    db_path, store = _prepare_database(tmp_path, [item])
    unit = _source_unit(item)
    store.insert_source(
        source_id=SOURCE_ID,
        workspace_id=WORKSPACE_ID,
        project_id=PROJECT_ID,
        visibility="private",
        owner_principal_id=PRINCIPAL_ID,
        source_path=unit["source_path"],
        source_sha256=unit["source_sha256"],
        source_type=unit["source_type"],
        sensitivity=unit["sensitivity"],
    )
    store.insert_source_unit(
        source_unit_id=unit["source_unit_id"],
        source_id=unit["source_id"],
        project_id=unit["project_id"],
        parent_unit_id=None,
        scope_type=unit["scope_type"],
        scope_ref=unit["scope_ref"],
        source_unit_hash=unit["source_unit_hash"],
        text_content="Conflicting local unit text.",
        metadata={"handoff_id": HANDOFF_ID},
    )
    with pytest.raises(SkillApiBlocked, match="existing source unit"):
        _apply_local(directory, db_path)
    with store.read() as connection:
        assert connection.execute("SELECT COUNT(*) FROM memory_items").fetchone()[0] == 0
        grant = connection.execute("SELECT grant_id,revoked FROM access_grants").fetchone()
    assert grant is not None
    assert grant["revoked"] == 1


def test_local_apply_requires_existing_migrated_and_scoped_database(tmp_path: Path) -> None:
    item = _item(key="mwh_local_db_validation")
    directory, _ = _write_fixture(tmp_path, [item], [(item, "approve", BASE_REVIEWED_AT)])

    with pytest.raises(SkillApiBlocked, match="does not exist"):
        _apply_local(directory, tmp_path / "missing.sqlite3")

    empty_db = tmp_path / "empty.sqlite3"
    empty_db.write_bytes(b"")
    with pytest.raises(SkillApiBlocked, match="not migrated"):
        _apply_local(directory, empty_db)

    corrupt_db = tmp_path / "corrupt.sqlite3"
    corrupt_db.write_bytes(b"not a sqlite database")
    with pytest.raises(SkillApiBlocked, match="canonical_db"):
        _apply_local(directory, corrupt_db)

    scoped_db = tmp_path / "scoped.sqlite3"
    scoped_store = SQLiteStore(scoped_db)
    scoped_store.migrate()
    scoped_store.register_profiles(ProfileRegistry())
    with pytest.raises(SkillApiBlocked, match="workspace_id"):
        _apply_local(directory, scoped_db)
    assert _counts(scoped_store) == (0, 0)

    profile_db = tmp_path / "profile.sqlite3"
    profile_store = SQLiteStore(profile_db)
    profile_store.migrate()
    profile_store.ensure_workspace_project(
        workspace_id=WORKSPACE_ID,
        workspace_name="Workspace",
        project_id=PROJECT_ID,
        project_name="Project",
    )
    with pytest.raises(SkillApiBlocked, match="not registered"):
        _apply_local(directory, profile_db)
    assert _counts(profile_store) == (0, 0)