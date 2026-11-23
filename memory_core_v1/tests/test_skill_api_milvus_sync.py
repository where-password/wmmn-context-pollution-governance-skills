from __future__ import annotations

from datetime import datetime, timezone

import pytest

from memory_core.delete import MemoryDeleteService
from memory_core.fakes import FakeEmbedding, FakeVectorIndex
from memory_core.models import MemoryDeleteInput
from memory_core.profiles import ProfileRegistry
from memory_core.skill_api import (
    DatabaseProfile,
    SkillApiBlocked,
    SkillBackend,
    sync_milvus_outbox,
)

from .conftest import add_source, make_grant, make_token, make_write_input

READ_ALIAS = "memory_read_alias_sync_test_001"
WRITE_ALIAS = "memory_write_alias_sync_test_001"
COLLECTION = "memory_core_sync_test_v1"


def _profile(store, *, read_alias: str = READ_ALIAS) -> DatabaseProfile:
    store.register_profiles(ProfileRegistry())
    return DatabaseProfile(
        environment="test",
        backend=SkillBackend.MILVUS,
        capabilities=("query", "health", "backup", "write"),
        workspace_id="ws1",
        project_ids=("p1",),
        domain_profile="novel",
        owner_principal_id="user-a",
        max_sensitivity="private",
        canonical_db=store.path,
        canonical_sha256="test-only",
        read_alias=read_alias,
        write_alias=WRITE_ALIAS,
        physical_collection=COLLECTION,
        embedding_model="BAAI/bge-m3",
        embedding_dimension=1024,
        index_schema_version="memory-index-v1",
        metric_type="COSINE",
        plan_id="milvus-sync-test",
        created_at=datetime.now(timezone.utc),
    )


def _write(store, writer, secret, *, content: str, key: str, supersedes_id: str | None = None):
    grant = make_grant(store)
    return writer.write(
        make_write_input(
            content=content,
            idempotency_key=key,
            supersedes_id=supersedes_id,
            expected_version=1 if supersedes_id else None,
        ),
        make_token(secret, grant),
    )


def test_sync_processes_outbox_upsert_and_revoke_delete(store, writer, secret) -> None:
    add_source(store)
    profile = _profile(store)
    index = FakeVectorIndex(read_alias=READ_ALIAS, write_alias=WRITE_ALIAS)
    embedding = FakeEmbedding(dimension=1024, model="BAAI/bge-m3")
    written = _write(store, writer, secret, content="同步写入事实", key="sync-upsert")

    first = sync_milvus_outbox(
        profile,
        fake=True,
        vector_index=index,
        embedding=embedding,
        owner="milvus-sync-test",
    )
    assert first.status == "completed"
    assert first.outbox_before == {"pending": 2}
    assert first.outbox_after == {"done": 2}
    assert first.processed.get("fts_synced") == 1
    assert first.processed.get("vector_synced") == 1
    assert first.embedding_calls == 1
    assert index.count(READ_ALIAS) == 1
    indexed_doc = next(iter(index.collections[READ_ALIAS].values()))
    assert indexed_doc.embedding_model == "BAAI/bge-m3"
    assert indexed_doc.embedding_dim == 1024
    assert indexed_doc.index_schema_version == "memory-index-v1"

    second = sync_milvus_outbox(
        profile,
        fake=True,
        vector_index=index,
        embedding=embedding,
        owner="milvus-sync-test",
    )
    assert second.status == "nothing_to_do"
    assert second.processed == {}

    grant = make_grant(store)
    delete_service = MemoryDeleteService(store=store, token_validator=writer.tokens)
    deleted = delete_service.delete(
        MemoryDeleteInput(
            idempotency_key="sync-delete",
            operation_id="sync-delete-op",
            workspace_id="ws1",
            target_type="memory_id",
            target_id=written.memory_id,
            delete_reason="sync revoke test",
            expected_version=1,
        ),
        make_token(secret, grant),
    )
    assert deleted.deleted_count == 1
    revoked = sync_milvus_outbox(
        profile,
        fake=True,
        vector_index=index,
        embedding=embedding,
        owner="milvus-sync-test",
    )
    assert revoked.status == "completed"
    assert index.count(READ_ALIAS) == 0
    assert revoked.outbox_after == {"done": 4}


def test_sync_reports_partial_batch_and_can_resume(store, writer, secret) -> None:
    add_source(store)
    profile = _profile(store)
    index = FakeVectorIndex(read_alias=READ_ALIAS, write_alias=WRITE_ALIAS)
    embedding = FakeEmbedding(dimension=1024, model="BAAI/bge-m3")
    _write(store, writer, secret, content="分批同步事实", key="sync-partial")

    first = sync_milvus_outbox(
        profile,
        fake=True,
        vector_index=index,
        embedding=embedding,
        limit=1,
    )
    assert first.status == "partial"
    assert first.outbox_after.get("pending") == 1
    second = sync_milvus_outbox(
        profile,
        fake=True,
        vector_index=index,
        embedding=embedding,
        limit=10,
    )
    assert second.status == "completed"
    assert "pending" not in second.outbox_after


def test_supersede_enqueues_old_delete_and_sync_keeps_only_new_vector(store, writer, secret) -> None:
    add_source(store)
    profile = _profile(store)
    index = FakeVectorIndex(read_alias=READ_ALIAS, write_alias=WRITE_ALIAS)
    embedding = FakeEmbedding(dimension=1024, model="BAAI/bge-m3")
    old = _write(store, writer, secret, content="旧决策事实", key="sync-old")
    assert sync_milvus_outbox(
        profile,
        fake=True,
        vector_index=index,
        embedding=embedding,
    ).status == "completed"
    assert index.count(READ_ALIAS) == 1

    new = _write(
        store,
        writer,
        secret,
        content="新决策事实",
        key="sync-new",
        supersedes_id=old.memory_id,
    )
    assert new.version == 2
    with store.read() as connection:
        delete_events = connection.execute(
            """SELECT target_type,operation,item_version FROM memory_outbox
               WHERE memory_id=? AND operation='delete' ORDER BY target_type""",
            (old.memory_id,),
        ).fetchall()
    assert [(row["target_type"], row["operation"], row["item_version"]) for row in delete_events] == [
        ("fts", "delete", 1),
        ("milvus", "delete", 1),
    ]

    synced = sync_milvus_outbox(
        profile,
        fake=True,
        vector_index=index,
        embedding=embedding,
        owner="milvus-sync-test",
    )
    assert synced.status == "completed"
    assert index.count(READ_ALIAS) == 1
    assert new.memory_id in index.collections[READ_ALIAS]
    assert old.memory_id not in index.collections[READ_ALIAS]


def test_real_sync_preflights_milvus_env_and_alias_targets(store, monkeypatch) -> None:
    profile = _profile(store)
    monkeypatch.delenv("MILVUS_SYNC_TEST_URI", raising=False)
    with pytest.raises(SkillApiBlocked, match="MILVUS_SYNC_TEST_URI"):
        sync_milvus_outbox(profile, uri_env="MILVUS_SYNC_TEST_URI")

    class BadAliasIndex:
        class Client:
            @staticmethod
            def has_collection(name):
                assert name == COLLECTION
                return True

        client = Client()

        def aliases(self):
            return {
                READ_ALIAS: "different_collection",
                WRITE_ALIAS: COLLECTION,
            }

    monkeypatch.setenv("MILVUS_SYNC_TEST_URI", "http://127.0.0.1:19530")
    monkeypatch.setattr(
        "memory_core.skill_api.milvus_sync.PymilvusMemoryIndex",
        lambda **_: BadAliasIndex(),
    )
    with pytest.raises(SkillApiBlocked, match="read alias"):
        sync_milvus_outbox(profile, uri_env="MILVUS_SYNC_TEST_URI")


def test_sync_rejects_production_names_and_missing_real_embedding_secret(store, writer, secret, monkeypatch) -> None:
    add_source(store)
    production = _profile(store, read_alias="memory_read_alias_sync_prod")
    with pytest.raises(SkillApiBlocked, match="non-production"):
        sync_milvus_outbox(production, fake=True)

    profile = _profile(store)
    _write(store, writer, secret, content="真实配置缺失测试", key="sync-real-missing")
    monkeypatch.delenv("TEST_MISSING_EMBEDDING_KEY", raising=False)
    with pytest.raises(SkillApiBlocked, match="TEST_MISSING_EMBEDDING_KEY"):
        sync_milvus_outbox(
            profile,
            fake=False,
            vector_index=FakeVectorIndex(read_alias=READ_ALIAS, write_alias=WRITE_ALIAS),
            embedding_key_env="TEST_MISSING_EMBEDDING_KEY",
        )
