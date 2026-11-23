from __future__ import annotations

from memory_core.db import utc_now_ts
from memory_core.fakes import FakeVectorIndex
from memory_core.models import MemoryIndexDoc, MemoryStatus, SensitivityLevel
from memory_core.ops import ReconcileService

from .conftest import add_source, make_grant, make_token, make_write_input


def _write_and_sync(store, writer, outbox, secret, *, content: str):
    add_source(store)
    result = writer.write(
        make_write_input(content=content),
        make_token(secret, make_grant(store)),
    )
    outbox.run_once(owner="initial-sync", limit=100, lease_seconds=60)
    return result


def _diff_types(plan: dict, resource: str) -> set[str]:
    return {
        item["difference_type"]
        for item in plan["resources"][resource]["differences"]
    }


def _insert_fts_ghost(store, *, source_memory_id: str, ghost_id: str, content: str) -> None:
    with store.transaction() as connection:
        connection.execute(
            """INSERT INTO memory_fts(
               memory_id,workspace_id,project_id,task_id,conversation_id,
               owner_principal_id,domain_profile,memory_type,status,sensitivity,
               visibility,content)
               SELECT ?,workspace_id,project_id,task_id,conversation_id,
                      owner_principal_id,domain_profile,memory_type,status,sensitivity,
                      visibility,?
               FROM memory_items WHERE memory_id=?""",
            (ghost_id, content, source_memory_id),
        )


def _insert_vector_ghost(vector_index, *, source_memory_id: str, ghost_id: str, content: str) -> None:
    template = vector_index.collections[vector_index.read_alias][source_memory_id]
    ghost = template.model_copy(
        update={
            "memory_id": ghost_id,
            "canonical_text": content,
            "embedding_text": content,
        }
    )
    for target in {vector_index.read_alias, vector_index.write_alias}:
        vector_index.collections[target][ghost_id] = ghost


def _update_vector(vector_index, memory_id: str, **updates) -> None:
    for target in {vector_index.read_alias, vector_index.write_alias}:
        doc = vector_index.collections[target][memory_id]
        vector_index.collections[target][memory_id] = doc.model_copy(update=updates)


def _make_index_doc(memory_id: str) -> MemoryIndexDoc:
    return MemoryIndexDoc(
        memory_id=memory_id,
        embedding=[0.0, 1.0],
        workspace_id="ws1",
        project_id="p1",
        owner_principal_id="user-a",
        domain_profile="novel",
        profile_version="1.0.0",
        memory_type="fact",
        status=MemoryStatus.ACTIVE,
        importance="medium",
        sensitivity=SensitivityLevel.PRIVATE,
        visibility="private",
        version=1,
        embedding_model="fake-bge-m3-v1",
        embedding_dim=2,
        index_schema_version="memory-index-v1",
        collection_alias="memory_read_alias",
        canonical_text=f"content {memory_id}",
        embedding_text=f"content {memory_id}",
        source_scope_json={},
        created_at_ts=1,
    )


class PagedTrackingVectorIndex(FakeVectorIndex):
    def __init__(self) -> None:
        super().__init__()
        self.requested_batch_sizes: list[int] = []

    def iter_rows(self, *, filter: str = "", batch_size: int = 1000, output_fields=None):
        self.requested_batch_sizes.append(batch_size)
        yield from super().iter_rows(
            filter=filter,
            batch_size=batch_size,
            output_fields=output_fields,
        )


def test_fts_same_version_repairs_missing_and_corrupt_rows(store, writer, outbox, secret) -> None:
    result = _write_and_sync(store, writer, outbox, secret, content="同版本 FTS 修复")

    with store.transaction() as connection:
        connection.execute("DELETE FROM memory_fts WHERE memory_id=?", (result.memory_id,))
        assert store.sync_fts_memory(result.memory_id, connection=connection) is True
        restored = connection.execute(
            "SELECT content FROM memory_fts WHERE memory_id=?", (result.memory_id,)
        ).fetchone()
        assert restored["content"] == "同版本 FTS 修复"

        connection.execute(
            "UPDATE memory_fts SET content='错误旧内容' WHERE memory_id=?", (result.memory_id,)
        )
        assert store.sync_fts_memory(result.memory_id, connection=connection) is True
        repaired = connection.execute(
            "SELECT content FROM memory_fts WHERE memory_id=?", (result.memory_id,)
        ).fetchone()
        assert repaired["content"] == "同版本 FTS 修复"

        assert store.sync_fts_memory(result.memory_id, connection=connection) is False


def test_same_count_delete_one_add_one_is_repaired(store, writer, outbox, secret) -> None:
    result = _write_and_sync(store, writer, outbox, secret, content="等量替换前的正典内容")
    ghost_id = "ghost-same-count"
    _insert_fts_ghost(
        store,
        source_memory_id=result.memory_id,
        ghost_id=ghost_id,
        content="幽灵 FTS 内容",
    )
    _insert_vector_ghost(
        outbox.vector_index,
        source_memory_id=result.memory_id,
        ghost_id=ghost_id,
        content="幽灵向量内容",
    )
    with store.transaction() as connection:
        connection.execute("DELETE FROM memory_fts WHERE memory_id=?", (result.memory_id,))
    for target in {outbox.vector_index.read_alias, outbox.vector_index.write_alias}:
        outbox.vector_index.collections[target].pop(result.memory_id)

    reconciler = ReconcileService(
        store=store,
        vector_index=outbox.vector_index,
        workspace_id="ws1",
        page_size=1,
    )
    plan = reconciler.plan()
    assert plan["expected_counts"] == {"fts": 1, "milvus": 1}
    assert plan["actual_counts"] == {"fts": 1, "milvus": 1}
    for resource in ("fts", "milvus"):
        assert {"missing", "extra_index_id"} <= _diff_types(plan, resource)

    applied = reconciler.apply(plan, explicit=True, worker=outbox, limit=20)
    assert applied["skipped_count"] == 0
    assert set(applied["selected_outbox_statuses"].values()) == {"done"}
    verified = reconciler.verify(plan)
    assert verified["passed"] is True
    assert verified["differences"] == []
    assert outbox.vector_index.collections[outbox.vector_index.read_alias].keys() == {result.memory_id}
    with store.read() as connection:
        fts_ids = {
            row["memory_id"] for row in connection.execute("SELECT memory_id FROM memory_fts")
        }
    assert fts_ids == {result.memory_id}


def test_stale_index_versions_are_repaired(store, writer, outbox, secret) -> None:
    result = _write_and_sync(store, writer, outbox, secret, content="版本落后修复")
    _update_vector(outbox.vector_index, result.memory_id, version=0)
    with store.transaction() as connection:
        connection.execute(
            "UPDATE memory_fts_sync_state SET item_version=0 WHERE memory_id=?",
            (result.memory_id,),
        )

    reconciler = ReconcileService(store=store, vector_index=outbox.vector_index, workspace_id="ws1")
    plan = reconciler.plan()
    assert _diff_types(plan, "fts") == {"stale_version"}
    assert _diff_types(plan, "milvus") == {"stale_version"}

    reconciler.apply(plan, explicit=True, worker=outbox, limit=20)
    assert reconciler.verify(plan)["passed"] is True
    doc = outbox.vector_index.collections[outbox.vector_index.read_alias][result.memory_id]
    assert doc.version == 1
    with store.read() as connection:
        sync_version = connection.execute(
            "SELECT item_version FROM memory_fts_sync_state WHERE memory_id=?",
            (result.memory_id,),
        ).fetchone()["item_version"]
    assert sync_version == 1


def test_same_version_fingerprint_mismatch_replays_completed_outbox(store, writer, outbox, secret) -> None:
    result = _write_and_sync(store, writer, outbox, secret, content="指纹一致的正典内容")
    _update_vector(outbox.vector_index, result.memory_id, canonical_text="错误向量文本")
    with store.transaction() as connection:
        connection.execute(
            "UPDATE memory_fts SET content='错误 FTS 文本' WHERE memory_id=?",
            (result.memory_id,),
        )

    reconciler = ReconcileService(store=store, vector_index=outbox.vector_index, workspace_id="ws1")
    plan = reconciler.plan()
    assert _diff_types(plan, "fts") == {"fingerprint_mismatch"}
    assert _diff_types(plan, "milvus") == {"fingerprint_mismatch"}

    applied = reconciler.apply(plan, explicit=True, worker=outbox, limit=20)
    assert applied["applied_count"] == 0
    assert applied["replayed_count"] == 2
    assert set(applied["replay_states"].values()) == {"replayed"}
    assert reconciler.verify(plan)["passed"] is True
    assert outbox.vector_index.collections[outbox.vector_index.read_alias][result.memory_id].canonical_text == "指纹一致的正典内容"
    with store.read() as connection:
        content = connection.execute(
            "SELECT content FROM memory_fts WHERE memory_id=?", (result.memory_id,)
        ).fetchone()["content"]
    assert content == "指纹一致的正典内容"


def test_ghost_index_ids_are_deleted_without_touching_canonical(store, writer, outbox, secret) -> None:
    result = _write_and_sync(store, writer, outbox, secret, content="保留的正典事实")
    ghost_id = "ghost-only"
    _insert_fts_ghost(
        store,
        source_memory_id=result.memory_id,
        ghost_id=ghost_id,
        content="幽灵 FTS",
    )
    _insert_vector_ghost(
        outbox.vector_index,
        source_memory_id=result.memory_id,
        ghost_id=ghost_id,
        content="幽灵向量",
    )

    reconciler = ReconcileService(store=store, vector_index=outbox.vector_index, workspace_id="ws1")
    plan = reconciler.plan()
    assert _diff_types(plan, "fts") == {"extra_index_id"}
    assert _diff_types(plan, "milvus") == {"extra_index_id"}

    reconciler.apply(plan, explicit=True, worker=outbox, limit=20)
    assert reconciler.verify(plan)["passed"] is True
    assert store.get_memory(result.memory_id) is not None
    assert ghost_id not in outbox.vector_index.collections[outbox.vector_index.read_alias]
    with store.read() as connection:
        assert connection.execute(
            "SELECT count(*) FROM memory_fts WHERE memory_id=?", (ghost_id,)
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT count(*) FROM memory_items WHERE memory_id=?", (ghost_id,)
        ).fetchone()[0] == 0


def test_apply_skips_operations_when_canonical_version_changes_after_plan(store, writer, outbox, secret) -> None:
    result = _write_and_sync(store, writer, outbox, secret, content="计划过期保护")
    for target in {outbox.vector_index.read_alias, outbox.vector_index.write_alias}:
        outbox.vector_index.collections[target].pop(result.memory_id)
    with store.transaction() as connection:
        connection.execute("DELETE FROM memory_fts WHERE memory_id=?", (result.memory_id,))

    reconciler = ReconcileService(store=store, vector_index=outbox.vector_index, workspace_id="ws1")
    plan = reconciler.plan()
    assert plan["passed"] is False

    with store.transaction() as connection:
        connection.execute(
            "UPDATE memory_items SET version=version+1,updated_at=? WHERE memory_id=?",
            (utc_now_ts() + 1, result.memory_id),
        )

    applied = reconciler.apply(plan, explicit=True, worker=outbox, limit=20)
    assert applied["applied_count"] == 0
    assert applied["replayed_count"] == 0
    assert applied["skipped_count"] == 2
    assert {item["skip_reason"] for item in applied["skipped"]} == {"skip_stale_plan"}
    assert outbox.vector_index.collections[outbox.vector_index.read_alias].get(result.memory_id) is None
    with store.read() as connection:
        assert connection.execute(
            "SELECT count(*) FROM memory_fts WHERE memory_id=?", (result.memory_id,)
        ).fetchone()[0] == 0
        assert connection.execute(
            """SELECT count(*) FROM memory_outbox
               WHERE memory_id=? AND item_version=2""",
            (result.memory_id,),
        ).fetchone()[0] == 0
    verified = reconciler.verify(plan)
    assert verified["snapshot_matches"] is False
    assert verified["passed"] is False


def test_reconcile_reads_more_than_1000_milvus_rows_in_pages(store) -> None:
    vector_index = PagedTrackingVectorIndex()
    for index in range(1001):
        memory_id = f"page-{index:04d}"
        vector_index.collections[vector_index.read_alias][memory_id] = _make_index_doc(memory_id)
    reconciler = ReconcileService(
        store=store,
        vector_index=vector_index,
        workspace_id="ws1",
        page_size=137,
    )
    plan = reconciler.plan()
    assert vector_index.requested_batch_sizes == [137]
    assert plan["actual_counts"]["milvus"] == 1001
    assert len(plan["operations"]) == 1001
    assert _diff_types(plan, "milvus") == {"extra_index_id"}


def test_fts_and_milvus_share_the_same_snapshot(store, writer, outbox, secret) -> None:
    _write_and_sync(store, writer, outbox, secret, content="同一快照")
    plan = ReconcileService(
        store=store,
        vector_index=outbox.vector_index,
        workspace_id="ws1",
    ).plan()
    assert plan["passed"] is True
    for resource in ("fts", "milvus"):
        assert plan["resources"][resource]["snapshot_id"] == plan["snapshot_id"]
        assert plan["resources"][resource]["snapshot_watermark"] == plan["snapshot_watermark"]
    assert (
        plan["resources"]["fts"]["snapshot_id"]
        == plan["resources"]["milvus"]["snapshot_id"]
    )
    assert (
        plan["resources"]["fts"]["snapshot_watermark"]
        == plan["resources"]["milvus"]["snapshot_watermark"]
    )


def test_repeated_apply_is_idempotent(store, writer, outbox, secret) -> None:
    result = _write_and_sync(store, writer, outbox, secret, content="重复 apply 幂等")
    _update_vector(outbox.vector_index, result.memory_id, canonical_text="错误文本")
    with store.transaction() as connection:
        connection.execute(
            "UPDATE memory_fts SET content='错误内容' WHERE memory_id=?",
            (result.memory_id,),
        )
    reconciler = ReconcileService(store=store, vector_index=outbox.vector_index, workspace_id="ws1")
    plan = reconciler.plan()

    first = reconciler.apply(plan, explicit=True, worker=outbox, limit=20)
    assert first["replayed_count"] == 2
    assert reconciler.verify(plan)["passed"] is True
    first_fts_count = store.reconciliation_counts(workspace_id="ws1")["fts_rows"]
    first_vector_count = outbox.vector_index.count(outbox.vector_index.read_alias)

    second = reconciler.apply(plan, explicit=True, worker=outbox, limit=20)
    assert second["replayed_count"] == 2
    assert reconciler.verify(plan)["passed"] is True
    assert store.reconciliation_counts(workspace_id="ws1")["fts_rows"] == first_fts_count == 1
    assert outbox.vector_index.count(outbox.vector_index.read_alias) == first_vector_count == 1
