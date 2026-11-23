from __future__ import annotations

import json
from pathlib import Path

import pytest

from memory_core.db import utc_now_ts
from memory_core.delete import MemoryDeleteService
from memory_core.fakes import FakeEmbedding, FakeVectorIndex
from memory_core.models import MemoryDeleteInput
from memory_core.ops import (
    JsonRunRecorder,
    OutboxAdmin,
    ReconcileService,
    SchedulerService,
    assert_isolated_db_path,
    assert_test_resource_names,
)
from memory_core.outbox import OutboxWorker

from .conftest import add_source, make_grant, make_token, make_write_input


def _write_one(store, writer, secret, *, content: str = "林岚确认灯塔密道通往北堤。"):
    grant = make_grant(store)
    return writer.write(make_write_input(content=content), make_token(secret, grant))


def _run_all(outbox: OutboxWorker) -> dict[str, int]:
    return outbox.run_once(owner="test-worker", limit=100, lease_seconds=60)


def test_claim_pending_claimed_and_expired_lease(store, writer, secret) -> None:
    add_source(store)
    _write_one(store, writer, secret)
    base = utc_now_ts()

    first = store.claim_outbox(owner="worker-1", limit=10, lease_seconds=10, now=base)
    assert len(first) == 2
    assert {event["status"] for event in first} == {"claimed"}

    while_leased = store.claim_outbox(owner="worker-2", limit=10, lease_seconds=10, now=base + 1)
    assert while_leased == []

    after_expiry = store.claim_outbox(owner="worker-3", limit=10, lease_seconds=10, now=base + 11)
    assert {event["outbox_id"] for event in after_expiry} == {event["outbox_id"] for event in first}
    assert {event["lease_owner"] for event in after_expiry} == {"worker-3"}


def test_fts_and_vector_upsert_and_delete_are_idempotent(store, writer, outbox, token_validator, secret) -> None:
    add_source(store)
    grant = make_grant(store)
    result = writer.write(make_write_input(content="幂等索引事实"), make_token(secret, grant))
    first = _run_all(outbox)
    assert first.get("fts_synced") == 1
    assert first.get("vector_synced") == 1
    assert outbox.vector_index.count(outbox.vector_index.read_alias) == 1

    with store.read() as connection:
        row = connection.execute(
            "SELECT outbox_id FROM memory_outbox WHERE target_type='milvus' LIMIT 1"
        ).fetchone()
    assert row is not None
    milvus_event = store.get_outbox(row["outbox_id"])
    with store.transaction() as connection:
        changed = store.sync_fts_memory(result.memory_id, connection=connection)
        duplicate_changed = store.sync_fts_memory(result.memory_id, connection=connection)
    assert changed is False
    assert duplicate_changed is False
    outbox.process_event(milvus_event)
    assert outbox.vector_index.count(outbox.vector_index.read_alias) == 1
    with store.read() as connection:
        assert connection.execute("SELECT count(*) FROM memory_fts").fetchone()[0] == 1

    delete_service = MemoryDeleteService(store=store, token_validator=token_validator)
    request = MemoryDeleteInput(
        idempotency_key="delete-idempotent",
        operation_id="delete-op-idempotent",
        workspace_id="ws1",
        target_type="memory_id",
        target_id=result.memory_id,
        delete_reason="ops idempotency test",
        expected_version=1,
    )
    deleted = delete_service.delete(request, make_token(secret, grant))
    assert deleted.deleted_count == 1
    _run_all(outbox)
    assert outbox.vector_index.count(outbox.vector_index.read_alias) == 0
    assert store.fts_search("幂等索引事实") == []
    assert _run_all(outbox) == {}


def test_worker_maps_logical_write_alias_to_configured_test_alias(store, writer, secret) -> None:
    add_source(store)
    grant = make_grant(store)
    result = writer.write(
        make_write_input(content="逻辑写别名映射事实"),
        make_token(secret, grant),
    )
    vector_index = FakeVectorIndex(
        read_alias="memory_read_alias_test_001",
        write_alias="memory_write_alias_test_001",
    )
    worker = OutboxWorker(
        store=store,
        profiles=writer.profiles,
        embedding=FakeEmbedding(),
        vector_index=vector_index,
        write_alias=vector_index.write_alias,
    )
    worker.run_once(owner="alias-mapper", limit=10, lease_seconds=60)
    assert vector_index.count(vector_index.write_alias) == 1
    with store.read() as connection:
        event_target = connection.execute(
            "SELECT target_name FROM memory_outbox WHERE memory_id=? AND target_type='milvus'",
            (result.memory_id,),
        ).fetchone()["target_name"]
    assert event_target == "memory_write_alias"


def test_worker_uses_injected_write_alias_for_any_upsert_or_delete_target(store, profiles) -> None:
    configured_alias = "custom_write_alias_test_001"
    vector_index = FakeVectorIndex(
        read_alias="custom_read_alias_test_001",
        write_alias=configured_alias,
    )
    worker = OutboxWorker(
        store=store,
        profiles=profiles,
        embedding=FakeEmbedding(),
        vector_index=vector_index,
        write_alias=configured_alias,
    )
    for operation in ("upsert", "delete"):
        assert worker._vector_target({
            "target_type": "milvus",
            "operation": operation,
            "target_name": "legacy_logical_alias",
        }) == configured_alias
    assert worker._vector_target({
        "target_type": "milvus",
        "operation": "reconcile",
        "target_name": "explicit_reconcile_target",
    }) == "explicit_reconcile_target"


def test_worker_ignores_production_looking_event_target_when_alias_is_injected(store, writer, secret) -> None:
    add_source(store)
    grant = make_grant(store)
    result = writer.write(
        make_write_input(content="拒绝生产别名目标"),
        make_token(secret, grant),
    )
    vector_index = FakeVectorIndex(
        read_alias="memory_read_alias_test_001",
        write_alias="memory_write_alias_test_001",
    )
    worker = OutboxWorker(
        store=store,
        profiles=writer.profiles,
        embedding=FakeEmbedding(),
        vector_index=vector_index,
    )
    with store.read() as connection:
        row = connection.execute(
            "SELECT * FROM memory_outbox WHERE memory_id=? AND target_type='milvus'",
            (result.memory_id,),
        ).fetchone()
    event = dict(row)
    event["target_name"] = "memory_write_alias_prod"
    assert worker.process_event(event) == "vector_synced"
    assert "memory_write_alias_prod" not in vector_index.collections
    assert vector_index.count(vector_index.write_alias) == 1


def test_poison_stops_automatic_retry_and_manual_resume(store, writer, secret) -> None:
    add_source(store)
    _write_one(store, writer, secret)
    worker = OutboxWorker(
        store=store,
        profiles=writer.profiles,
        embedding=FakeEmbedding(),
        vector_index=FakeVectorIndex(),
        max_retries=1,
    )
    worker.vector_index.fail_remaining = 10
    with store.read() as connection:
        rows = [dict(row) for row in connection.execute("SELECT * FROM memory_outbox ORDER BY target_type")]
    fts_event = next(row for row in rows if row["target_type"] == "fts")
    milvus_event = next(row for row in rows if row["target_type"] == "milvus")
    store.mark_outbox_done(fts_event["outbox_id"])

    first = worker.process_claimed([milvus_event])
    assert first == {"pending": 1}
    pending_event = store.get_outbox(milvus_event["outbox_id"])
    assert pending_event["status"] == "pending"
    assert pending_event["next_retry_at"] - pending_event["updated_at"] == 2
    second = worker.process_claimed([milvus_event])
    assert second == {"poison": 1}
    event = store.get_outbox(milvus_event["outbox_id"])
    assert event["status"] == "poison"
    assert event["retry_count"] == 2

    assert worker.run_once(owner="should-not-retry", limit=10, lease_seconds=60) == {}
    assert store.get_outbox(milvus_event["outbox_id"])["retry_count"] == 2

    worker.vector_index.fail_remaining = 0
    admin = OutboxAdmin(store)
    resumed = admin.resume_poison(milvus_event["outbox_id"])
    assert resumed["status"] == "pending"
    assert resumed["retry_count"] == 0
    worker.run_once(owner="manual-replay", limit=10, lease_seconds=60)
    assert store.get_outbox(milvus_event["outbox_id"])["status"] == "done"
    assert worker.vector_index.count(worker.vector_index.read_alias) == 1


def test_ttl_cleanup_deletes_only_expired_and_safe_tombstones(store, writer, secret) -> None:
    add_source(store, source_id="src-ttl-old", source_unit_id="unit-ttl-old", text="林岚在旧灯塔发现密道。")
    add_source(store, source_id="src-ttl-live", source_unit_id="unit-ttl-live", text="林岚在旧灯塔发现密道。")
    grant = make_grant(store)
    old = writer.write(
        make_write_input(
            source_id="src-ttl-old",
            source_unit_id="unit-ttl-old",
            content="已过期墓碑事实",
            idempotency_key="ttl-old",
        ),
        make_token(secret, grant),
    )
    live = writer.write(
        make_write_input(
            source_id="src-ttl-live",
            source_unit_id="unit-ttl-live",
            content="仍然有效的事实",
            idempotency_key="ttl-live",
        ),
        make_token(secret, grant),
    )
    recent = writer.write(
        make_write_input(
            source_id="src-ttl-live",
            source_unit_id="unit-ttl-live",
            content="刚进入墓碑保护期的事实",
            idempotency_key="ttl-recent",
        ),
        make_token(secret, grant),
    )
    now = utc_now_ts()
    with store.transaction() as connection:
        connection.execute(
            "UPDATE memory_items SET deleted_at=?,version=version+1 WHERE memory_id=?",
            (now - 40 * 86400, old.memory_id),
        )
        connection.execute(
            "UPDATE memory_items SET deleted_at=?,version=version+1 WHERE memory_id=?",
            (now - 86400, recent.memory_id),
        )
        for wm_id, ttl in (("wm-expired", now - 1), ("wm-live", now + 3600)):
            connection.execute(
                """INSERT INTO working_memory(
                   wm_id,workspace_id,project_id,task_id,conversation_id,owner_principal_id,
                   domain_profile,memory_type,subtype,normalized_subject,content,sensitivity,
                   visibility,confidence,ttl_expires_at,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (wm_id, "ws1", "p1", "ttl-task", "conv1", "user-a", "novel",
                 "fact", "narrative_fact", "", f"content {wm_id}", "private",
                 "private", 0.5, ttl, now),
            )
    cleaned = store.cleanup_ttl(now=now, tombstone_retention_seconds=30 * 86400)
    assert cleaned["working_memory"] == 1
    assert cleaned["tombstones"] == 1
    assert store.get_memory(old.memory_id) is None
    assert store.get_memory(live.memory_id) is not None
    assert store.get_memory(recent.memory_id)["deleted_at"] is not None
    with store.read() as connection:
        wm_ids = {row["wm_id"] for row in connection.execute("SELECT wm_id FROM working_memory")}
        evidence_count = connection.execute(
            "SELECT count(*) FROM memory_evidence WHERE memory_id=?", (old.memory_id,)
        ).fetchone()[0]
    assert wm_ids == {"wm-live"}
    assert evidence_count == 0


def test_reconcile_reports_missing_without_deleting_valid_data(store, writer, outbox, secret) -> None:
    add_source(store)
    grant = make_grant(store, principal_id="reconcile-user")
    result = writer.write(make_write_input(content="对账不应删除的事实"), make_token(secret, grant))
    _run_all(outbox)
    outbox.vector_index.collections[outbox.vector_index.read_alias].pop(result.memory_id)
    with store.transaction() as connection:
        connection.execute("DELETE FROM memory_fts WHERE memory_id=?", (result.memory_id,))
    before_delete_calls = outbox.vector_index.delete_calls
    report = ReconcileService(store=store, vector_index=outbox.vector_index, workspace_id="ws1").run()
    assert report["passed"] is False
    assert {issue["resource"] for issue in report["issues"]} == {"fts", "milvus"}
    assert report["deletions_performed"] == 0
    assert outbox.vector_index.delete_calls == before_delete_calls
    assert store.get_memory(result.memory_id) is not None
    assert store.get_memory_record(result.memory_id).status.value == "active"


def test_scheduler_records_success_and_error(tmp_path: Path, store, writer, outbox, secret) -> None:
    add_source(store)
    grant = make_grant(store)
    writer.write(make_write_input(content="调度器运行事实"), make_token(secret, grant))
    _run_all(outbox)
    recorder = JsonRunRecorder(tmp_path / "runtime")
    reconciler = ReconcileService(store=store, vector_index=outbox.vector_index, workspace_id="ws1")
    scheduler = SchedulerService(worker=outbox, store=store, reconciler=reconciler, recorder=recorder)
    result = scheduler.run_once(owner="scheduler-test", limit=10, lease_seconds=60)
    assert result["status"] == "ok"
    assert (tmp_path / "runtime" / "latest_scheduler_run.json").is_file()
    assert (tmp_path / "runtime" / "scheduler_runs.jsonl").read_text(encoding="utf-8").strip()

    class FailingWorker:
        def run_once(self, **_: object) -> dict[str, int]:
            raise RuntimeError("synthetic worker failure")

    failing = SchedulerService(
        worker=FailingWorker(), store=store, reconciler=reconciler, recorder=recorder
    ).run_once(owner="scheduler-error", limit=10, lease_seconds=60)
    assert failing["status"] == "error"
    assert failing["errors"][0]["message"] == "synthetic worker failure"
    latest_error = json.loads(
        (tmp_path / "runtime" / "latest_scheduler_run.json").read_text(encoding="utf-8")
    )
    assert latest_error["status"] == "error"
    assert latest_error["errors"][0]["type"] == "RuntimeError"


def test_runtime_entry_rejects_production_aliases() -> None:
    assert_test_resource_names(
        read_alias="memory_read_alias_test_001",
        write_alias="memory_write_alias_test_001",
        collection_name="memory_core_stress105_v5_v1",
    )
    with pytest.raises(PermissionError):
        assert_test_resource_names(
            read_alias="memory_read_alias_prod",
            write_alias="memory_write_alias_prod",
            collection_name="memory_core_stress105_v5_v1",
        )
    with pytest.raises(PermissionError):
        assert_isolated_db_path(r"C:\example\stress_test_001\work\sqlite\stress_105_v5.sqlite3")
