from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from memory_core.db import SQLiteStore
from memory_core.models import AccessGrant, AccessScope, MemoryStatus, SensitivityLevel
from memory_core.profiles import ProfileRegistry
from memory_core.security import TokenValidator, issue_token
from memory_core.skill_api import SkillApiBlocked, apply_handoff_ingest

from .test_skill_api_handoff import (
    BASE_REVIEWED_AT,
    DOMAIN_PROFILE,
    HANDOFF_ID,
    PROFILE_VERSION,
    PROJECT_ID,
    SOURCE_ID,
    WORKSPACE_ID,
    _item,
    _rewrite_source_units,
    _source_unit,
    _write_fixture,
)

SECRET = b"handoff-apply-test-secret"


def _runtime(tmp_path: Path, items: list[dict], *, actions: list[str] | None = None):
    store = SQLiteStore(tmp_path / "apply-memory.sqlite3")
    store.migrate()
    store.ensure_workspace_project(
        workspace_id=WORKSPACE_ID,
        workspace_name="Handoff Workspace",
        project_id=PROJECT_ID,
        project_name="Handoff Project",
    )

    now = datetime.now(timezone.utc)
    grant = AccessGrant(
        grant_id="grant-handoff-apply",
        principal_id="handoff-user",
        workspace_id=WORKSPACE_ID,
        scope=AccessScope(
            project_ids=[PROJECT_ID],
            domains=[DOMAIN_PROFILE],
            max_sensitivity=SensitivityLevel.PRIVATE,
        ),
        actions=actions or ["search", "read", "write", "delete"],
        expires_at=now + timedelta(hours=1),
        purpose="handoff apply test",
        created_at=now,
    )
    store.save_grant(grant)
    validator = TokenValidator(store, secret=SECRET)
    token = issue_token(
        secret=SECRET,
        principal_id="handoff-user",
        task_id="handoff-apply-task",
        grant_id=grant.grant_id,
        workspace_id=WORKSPACE_ID,
        ttl_seconds=600,
    )
    return store, ProfileRegistry(), validator, token


def _apply(directory: Path, runtime, written_memory_map=None):
    store, profiles, validator, token = runtime
    return apply_handoff_ingest(
        handoff_dir=directory,
        handoff_id=HANDOFF_ID,
        workspace_id=WORKSPACE_ID,
        project_id=PROJECT_ID,
        domain_profile=DOMAIN_PROFILE,
        profile_version=PROFILE_VERSION,
        source_id=SOURCE_ID,
        written_memory_map={} if written_memory_map is None else written_memory_map,
        store=store,
        profiles=profiles,
        token_validator=validator,
        token=token,
    )


def test_apply_approve_writes_active_memory_evidence_and_handoff_metadata(tmp_path: Path) -> None:
    item = _item()
    directory, _ = _write_fixture(tmp_path, [item], [(item, "approve", BASE_REVIEWED_AT)])
    runtime = _runtime(tmp_path, [item])
    result = _apply(directory, runtime)
    store = runtime[0]
    assert result.status == "completed"
    assert result.counts.writes_succeeded == 1
    assert result.counts.deletes_succeeded == 0
    assert result.before_counts["active_total"] == 0
    assert result.after_counts["active_total"] == 1
    assert result.write_results[0].idempotency_key == item["idempotency_key"]
    record = store.get_memory_record(result.write_results[0].memory_id)
    assert record.status == MemoryStatus.ACTIVE
    assert record.version == 1
    assert record.domain_payload["conversation_handoff"]["handoff_id"] == HANDOFF_ID
    assert record.domain_payload["conversation_handoff"]["claim_key"] == item["claim_key"]
    assert record.evidences and record.evidences[0].status.value == "verified"
    with store.read() as connection:
        source = connection.execute("SELECT * FROM sources WHERE source_id=?", (SOURCE_ID,)).fetchone()
        unit = connection.execute("SELECT * FROM source_units WHERE source_unit_id=?", (item["source_unit_id"],)).fetchone()
    assert source is not None
    assert source["visibility"] == "private"
    assert source["sensitivity"] == "private"
    assert source["source_sha256"] == "1" * 64
    assert source["owner_principal_id"] == "handoff-user"
    assert unit is not None
    metadata = json.loads(unit["metadata_json"])
    assert metadata["conversation_id"] == item["conversation_id"]
    assert metadata["message_id"] == item["evidences"][0]["locator_json"]["message_id"]
    assert metadata["source_type"] == "conversation_handoff_test"


def test_apply_registers_parent_unit_id_and_metadata(tmp_path: Path) -> None:
    item = _item(key="mwh_parent_unit")
    directory, _ = _write_fixture(tmp_path, [item], [(item, "approve", BASE_REVIEWED_AT)])
    unit = _source_unit(item)
    unit["parent_unit_id"] = "parent_source_unit_apply"
    unit["metadata"]["parent_unit_id"] = "parent_source_unit_apply"
    _rewrite_source_units(directory, [unit])
    runtime = _runtime(tmp_path, [item])
    result = _apply(directory, runtime)
    assert result.status == "completed"
    with runtime[0].read() as connection:
        row = connection.execute(
            "SELECT parent_unit_id,metadata_json FROM source_units WHERE source_unit_id=?",
            (unit["source_unit_id"],),
        ).fetchone()
    assert row is not None
    assert row["parent_unit_id"] == "parent_source_unit_apply"
    assert json.loads(row["metadata_json"])["parent_unit_id"] == "parent_source_unit_apply"


def test_apply_repeat_is_idempotent_without_duplicate_rows(tmp_path: Path) -> None:
    item = _item()
    directory, _ = _write_fixture(tmp_path, [item], [(item, "approve", BASE_REVIEWED_AT)])
    runtime = _runtime(tmp_path, [item])
    first = _apply(directory, runtime)
    second = _apply(directory, runtime)
    store = runtime[0]
    assert first.write_results[0].memory_id == second.write_results[0].memory_id
    with store.read() as connection:
        count = connection.execute("SELECT COUNT(*) FROM memory_items WHERE workspace_id=?", (WORKSPACE_ID,)).fetchone()[0]
        source_count = connection.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
        unit_count = connection.execute("SELECT COUNT(*) FROM source_units").fetchone()[0]
    assert count == 1
    assert source_count == 1
    assert unit_count == 1
    assert second.counts.writes_succeeded == 1


def test_apply_supersedes_uses_written_map_and_marks_old_record(tmp_path: Path) -> None:
    old = _item(key="mwh_old_apply")
    new = _item(key="mwh_new_apply", content="Updated handoff fact.", supersedes_ref=old["idempotency_key"])
    runtime = _runtime(tmp_path, [old, new])
    old_fixture, _ = _write_fixture(tmp_path / "old-handoff", [old], [(old, "approve", BASE_REVIEWED_AT)])
    old_result = _apply(old_fixture, runtime)
    old_memory_id = old_result.write_results[0].memory_id
    old_version = old_result.write_results[0].version

    combined, _ = _write_fixture(
        tmp_path / "combined-handoff",
        [old, new],
        [(old, "approve", BASE_REVIEWED_AT), (new, "approve", "2026-11-22T11:00:00+08:00")],
    )
    result = _apply(combined, runtime, {old["idempotency_key"]: {"memory_id": old_memory_id, "version": old_version}})
    store = runtime[0]
    assert result.counts.writes_succeeded == 1
    new_record = store.get_memory_record(result.write_results[0].memory_id)
    old_record = store.get_memory_record(old_memory_id)
    assert old_record.status == MemoryStatus.SUPERSEDED
    assert new_record.supersedes_id == old_memory_id
    assert new_record.version == old_version + 1
    assert new_record.domain_payload["conversation_handoff"]["transition_kind"] == "supersedes"


def test_apply_revoke_with_written_map_soft_deletes_row(tmp_path: Path) -> None:
    item = _item(key="mwh_revoke_apply")
    runtime = _runtime(tmp_path, [item])
    approve_fixture, _ = _write_fixture(tmp_path / "approve", [item], [(item, "approve", BASE_REVIEWED_AT)])
    written = _apply(approve_fixture, runtime)
    memory_id = written.write_results[0].memory_id
    revoke_fixture, _ = _write_fixture(
        tmp_path / "revoke",
        [item],
        [(item, "approve", "2026-11-22T09:00:00+08:00"), (item, "revoke", "2026-11-22T10:00:00+08:00")],
    )
    result = _apply(revoke_fixture, runtime, {item["idempotency_key"]: {"memory_id": memory_id, "version": 1}})
    store = runtime[0]
    record = store.get_memory_record(memory_id)
    assert result.counts.deletes_succeeded == 1
    assert result.delete_results[0].memory_ids == [memory_id]
    assert record is not None
    assert record.deleted_at is not None


def test_apply_revoke_without_map_remains_pending(tmp_path: Path) -> None:
    item = _item(key="mwh_pending_revoke")
    runtime = _runtime(tmp_path, [item])
    approve_fixture, _ = _write_fixture(tmp_path / "approve", [item], [(item, "approve", "2026-11-22T09:00:00+08:00")])
    _apply(approve_fixture, runtime)
    revoke_fixture, _ = _write_fixture(
        tmp_path / "revoke",
        [item],
        [(item, "approve", "2026-11-22T09:00:00+08:00"), (item, "revoke", "2026-11-22T10:00:00+08:00")],
    )
    result = _apply(revoke_fixture, runtime, {})
    assert result.counts.deletes_succeeded == 0
    assert result.counts.pending_post_write_revocations == 1


def test_apply_source_and_unit_conflicts_fail_before_memory_write(tmp_path: Path) -> None:
    item = _item(key="mwh_source_conflict")
    directory, _ = _write_fixture(tmp_path, [item], [(item, "approve", BASE_REVIEWED_AT)])
    unit = _source_unit(item)

    runtime = _runtime(tmp_path / "source-hash", [item])
    store = runtime[0]
    store.insert_source(
        source_id=SOURCE_ID,
        workspace_id=WORKSPACE_ID,
        project_id=PROJECT_ID,
        visibility="private",
        owner_principal_id="handoff-user",
        source_path=unit["source_path"],
        source_sha256="2" * 64,
        source_type=unit["source_type"],
        sensitivity=unit["sensitivity"],
    )
    with pytest.raises(SkillApiBlocked, match="existing source"):
        _apply(directory, runtime)
    with store.read() as connection:
        assert connection.execute("SELECT COUNT(*) FROM memory_items").fetchone()[0] == 0

    runtime = _runtime(tmp_path / "visibility", [item])
    store = runtime[0]
    store.insert_source(
        source_id=SOURCE_ID,
        workspace_id=WORKSPACE_ID,
        project_id=PROJECT_ID,
        visibility="workspace",
        owner_principal_id=None,
        source_path=unit["source_path"],
        source_sha256=unit["source_sha256"],
        source_type=unit["source_type"],
        sensitivity=unit["sensitivity"],
    )
    with pytest.raises(SkillApiBlocked, match="existing source"):
        _apply(directory, runtime)
    with store.read() as connection:
        assert connection.execute("SELECT COUNT(*) FROM memory_items").fetchone()[0] == 0

    runtime = _runtime(tmp_path / "sensitivity", [item])
    store = runtime[0]
    store.insert_source(
        source_id=SOURCE_ID,
        workspace_id=WORKSPACE_ID,
        project_id=PROJECT_ID,
        visibility="private",
        owner_principal_id="handoff-user",
        source_path=unit["source_path"],
        source_sha256=unit["source_sha256"],
        source_type=unit["source_type"],
        sensitivity="internal",
    )
    with pytest.raises(SkillApiBlocked, match="existing source"):
        _apply(directory, runtime)
    with store.read() as connection:
        assert connection.execute("SELECT COUNT(*) FROM memory_items").fetchone()[0] == 0

    runtime = _runtime(tmp_path / "unit-text", [item])
    store = runtime[0]
    store.insert_source(
        source_id=SOURCE_ID,
        workspace_id=WORKSPACE_ID,
        project_id=PROJECT_ID,
        visibility="private",
        owner_principal_id="handoff-user",
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
        text_content="Conflicting unit text.",
        metadata={"handoff_id": HANDOFF_ID},
    )
    with pytest.raises(SkillApiBlocked, match="existing source unit"):
        _apply(directory, runtime)
    with store.read() as connection:
        assert connection.execute("SELECT COUNT(*) FROM memory_items").fetchone()[0] == 0

    runtime = _runtime(tmp_path / "unit-scope", [item])
    store = runtime[0]
    store.insert_source(
        source_id=SOURCE_ID,
        workspace_id=WORKSPACE_ID,
        project_id=PROJECT_ID,
        visibility="private",
        owner_principal_id="handoff-user",
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
        scope_ref="conflicting-scope",
        source_unit_hash=unit["source_unit_hash"],
        text_content=unit["text_content"],
        metadata={"handoff_id": HANDOFF_ID},
    )
    with pytest.raises(SkillApiBlocked, match="existing source unit"):
        _apply(directory, runtime)
    with store.read() as connection:
        assert connection.execute("SELECT COUNT(*) FROM memory_items").fetchone()[0] == 0


def test_apply_nonapproved_items_do_not_write(tmp_path: Path) -> None:
    items = [_item(key="mwh_pending_apply"), _item(key="mwh_reject_apply"), _item(key="mwh_defer_apply")]
    directory, _ = _write_fixture(
        tmp_path,
        items,
        [(items[1], "reject", BASE_REVIEWED_AT), (items[2], "defer", BASE_REVIEWED_AT)],
    )
    runtime = _runtime(tmp_path, items, actions=["search", "read", "write", "delete"])
    result = _apply(directory, runtime)
    store = runtime[0]
    assert result.counts.writes_succeeded == 0
    assert result.counts.blocked_items == 3
    with store.read() as connection:
        assert connection.execute("SELECT COUNT(*) FROM memory_items").fetchone()[0] == 0


def test_apply_hash_tamper_blocks_before_any_sqlite_write(tmp_path: Path) -> None:
    item = _item(key="mwh_tamper_apply")
    directory, _ = _write_fixture(tmp_path, [item], [(item, "approve", BASE_REVIEWED_AT)])
    (directory / "memory_write_batch.jsonl").write_text(
        (directory / "memory_write_batch.jsonl").read_text(encoding="utf-8") + "\n",
        encoding="utf-8",
    )
    runtime = _runtime(tmp_path, [item])
    with pytest.raises(SkillApiBlocked, match="artifact hash"):
        _apply(directory, runtime)
    with runtime[0].read() as connection:
        assert connection.execute("SELECT COUNT(*) FROM memory_items").fetchone()[0] == 0


def test_apply_token_action_mismatch_writes_nothing(tmp_path: Path) -> None:
    item = _item(key="mwh_token_apply")
    directory, _ = _write_fixture(tmp_path, [item], [(item, "approve", BASE_REVIEWED_AT)])
    runtime = _runtime(tmp_path, [item], actions=["search", "read"])
    with pytest.raises(PermissionError, match="action"):
        _apply(directory, runtime)
    with runtime[0].read() as connection:
        assert connection.execute("SELECT COUNT(*) FROM memory_items").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM source_units").fetchone()[0] == 0


def test_apply_partial_failure_stops_and_is_safe_to_retry(tmp_path: Path) -> None:
    first = _item(key="mwh_partial_1", content="First valid handoff item.")
    second = _item(key="mwh_partial_2", content="Second handoff item with a missing supersedes target.")
    second["supersedes_id"] = "mem_missing_partial_target"
    directory, _ = _write_fixture(
        tmp_path,
        [first, second],
        [(first, "approve", BASE_REVIEWED_AT), (second, "approve", "2026-11-22T11:00:00+08:00")],
    )
    runtime = _runtime(tmp_path, [first, second])
    result = _apply(directory, runtime)
    store = runtime[0]
    assert result.status == "stopped"
    assert result.stopped_on_item is not None
    assert result.stopped_on_item.idempotency_key == second["idempotency_key"]
    assert result.applied_before_stop is not None
    assert result.applied_before_stop.writes_succeeded == 1
    with store.read() as connection:
        assert connection.execute("SELECT COUNT(*) FROM memory_items").fetchone()[0] == 1

    retry = _apply(directory, runtime)
    assert retry.status == "stopped"
    with store.read() as connection:
        assert connection.execute("SELECT COUNT(*) FROM memory_items").fetchone()[0] == 1


def test_apply_token_workspace_mismatch_writes_nothing(tmp_path: Path) -> None:
    item = _item(key="mwh_token_workspace_apply")
    directory, _ = _write_fixture(tmp_path, [item], [(item, "approve", BASE_REVIEWED_AT)])
    store, profiles, validator, _ = _runtime(tmp_path, [item])
    wrong_workspace_token = issue_token(
        secret=SECRET,
        principal_id="handoff-user",
        task_id="handoff-wrong-workspace",
        grant_id="grant-handoff-apply",
        workspace_id="wrong_workspace",
        ttl_seconds=600,
    )
    with pytest.raises(PermissionError, match="principal/workspace"):
        apply_handoff_ingest(
            handoff_dir=directory,
            handoff_id=HANDOFF_ID,
            workspace_id=WORKSPACE_ID,
            project_id=PROJECT_ID,
            domain_profile=DOMAIN_PROFILE,
            profile_version=PROFILE_VERSION,
            source_id=SOURCE_ID,
            written_memory_map={},
            store=store,
            profiles=profiles,
            token_validator=validator,
            token=wrong_workspace_token,
        )
    with store.read() as connection:
        assert connection.execute("SELECT COUNT(*) FROM memory_items").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 0
