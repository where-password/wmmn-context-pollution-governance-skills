from __future__ import annotations

from datetime import datetime, timezone

import pytest

from memory_core.models import EvidenceStatus
from memory_core.skill_api import (
    CapabilitySet,
    ConnectionProfile,
    DatabaseProfile,
    SkillApiBlocked,
    SkillBackend,
    build_context_pack,
    connection_profile_to_database_profile,
    get_memory,
    health_check,
    load_database_profile,
    query_ask,
    query_context,
    search_memory,
)

from .conftest import add_source, make_grant, make_token, make_write_input


def _profile(store, *, owner: str = "user-a") -> DatabaseProfile:
    return DatabaseProfile(
        environment="test",
        backend=SkillBackend.SQLITE,
        capabilities=("query", "health", "backup"),
        workspace_id="ws1",
        project_ids=("p1",),
        domain_profile="novel",
        owner_principal_id=owner,
        max_sensitivity="private",
        canonical_db=store.path,
        canonical_sha256="test-only",
        plan_id="test-plan",
        created_at=datetime.now(timezone.utc),
    )


def _write_visible_memory(store, writer, secret, outbox) -> str:
    add_source(store)
    grant = make_grant(store)
    item = make_write_input(content="林岚得知旧灯塔密道通往北堤。")
    result = writer.write(item, make_token(secret, grant))
    assert result.status.value == "active"
    outbox.run_once(owner="skill-api-test", limit=20)
    return result.memory_id


class FakeEmbedding:
    def embed_one(self, text: str) -> list[float]:
        assert text
        return [0.0] * 1024


class FakeReadIndex:
    def __init__(self, memory_id: str, collection: str = "memory_core_novel_test_v1") -> None:
        self.memory_id = memory_id
        self.collection = collection
        self.last_filter = ""

    def search_ids(self, embedding, *, filter="", limit=100):
        assert len(embedding) == 1024
        self.last_filter = filter
        return [(self.memory_id, 0.99)]

    def aliases(self):
        return {"memory_read_alias_novel_test_001": self.collection}

    def describe_collection(self):
        return {"collection_name": self.collection}

    def iter_rows(self, *, filter="", batch_size=1000, output_fields=None):
        yield {
            "memory_id": self.memory_id,
            "embedding_model": "BAAI/bge-m3",
            "embedding_dim": 1024,
            "index_schema_version": "memory-index-v1",
        }

    def count(self, *, filter=""):
        return 1


def _connection_profile(store, tmp_path, *, fts: str = "available") -> ConnectionProfile:
    return ConnectionProfile(
        mode="real",
        environment="test",
        backend="sqlite",
        workspace_root=store.path.parent,
        canonical_db=store.path.name,
        capabilities=CapabilitySet(sqlite="installed", fts=fts),
        workspace_id="ws1",
        project_ids=("p1",),
        domain_profile="novel",
        owner_principal_id="user-a",
        max_sensitivity="private",
        created_at=datetime.now(timezone.utc),
    )


def test_connection_profile_adapts_to_query_profile(store, tmp_path) -> None:
    profile = _connection_profile(store, tmp_path)
    adapted = connection_profile_to_database_profile(profile)
    assert adapted.backend == SkillBackend.SQLITE
    assert adapted.canonical_db == store.path.resolve()
    assert adapted.workspace_id == "ws1"
    assert adapted.project_ids == ("p1",)
    assert adapted.owner_principal_id == "user-a"
    assert "query" in adapted.capabilities
    assert "health" in adapted.capabilities
    assert len(adapted.canonical_sha256) == 64

    profile_path = tmp_path / "connection-profile.json"
    profile_path.write_text(profile.model_dump_json(indent=2) + "\n", encoding="utf-8")
    loaded = load_database_profile(profile_path)
    assert loaded == adapted


def test_search_and_get_memory_use_connection_profile_without_answering(
    store, writer, secret, outbox, tmp_path
) -> None:
    memory_id = _write_visible_memory(store, writer, secret, outbox)
    profile_path = tmp_path / "connection-profile.json"
    profile_path.write_text(
        _connection_profile(store, tmp_path).model_dump_json(indent=2) + "\n",
        encoding="utf-8",
    )

    searched = search_memory(profile_path, query="旧灯塔", top_k=5)
    assert searched.mode == "search_only"
    assert searched.hits
    assert "memory_id" not in searched.hits[0].model_dump()

    detail = get_memory(profile_path, memory_id=memory_id)
    assert detail.mode == "memory_detail"
    assert detail.content.startswith("林岚得知旧灯塔")
    assert detail.memory_id == memory_id
    assert detail.evidences
    assert all(evidence.quote is None for evidence in detail.evidences)

    detailed_quote = get_memory(profile_path, memory_id=memory_id, include_evidence_quote=True)
    assert any(evidence.quote for evidence in detailed_quote.evidences)


def test_connection_profile_rejects_unsafe_or_unqueryable_shape(store, tmp_path) -> None:
    escaped = _connection_profile(store, tmp_path).model_copy(update={"canonical_db": "../escape.sqlite3"})
    with pytest.raises(SkillApiBlocked, match="parent traversal"):
        connection_profile_to_database_profile(escaped)

    profile = _connection_profile(store, tmp_path, fts="unavailable")
    adapted = connection_profile_to_database_profile(profile)
    assert "health" in adapted.capabilities
    assert "query" not in adapted.capabilities
    with pytest.raises(SkillApiBlocked, match="capabilities do not allow query"):
        search_memory(profile, query="旧灯塔")


def test_sqlite_query_context_and_extractive_answer(store, writer, secret, outbox) -> None:
    _write_visible_memory(store, writer, secret, outbox)
    profile = _profile(store)

    context = query_context(profile, query="旧灯塔", top_k=5)
    assert context.evidence_completeness == "partial"
    assert context.hits
    assert context.hits[0].content.startswith("林岚得知旧灯塔")
    assert "memory_id" not in context.hits[0].model_dump()

    answer = query_ask(profile, query="旧灯塔", top_k=5)
    assert answer.synthesized is False
    assert answer.evidence_completeness == "partial"
    assert "旧灯塔" in answer.answer
    assert answer.source_ranges


def test_short_cjk_query_uses_substring_fallback(store, writer, secret, outbox) -> None:
    _write_visible_memory(store, writer, secret, outbox)
    context = query_context(_profile(store), query="灯塔", top_k=5)
    assert context.hits
    assert context.hits[0].content.startswith("林岚得知旧灯塔")


def test_private_owner_scope_is_enforced(store, writer, secret, outbox) -> None:
    _write_visible_memory(store, writer, secret, outbox)
    other_owner = _profile(store, owner="user-b")
    result = query_context(other_owner, query="旧灯塔")
    assert result.hits == []


def test_get_memory_enforces_owner_sensitivity_and_visibility(store, writer, secret, outbox) -> None:
    memory_id = _write_visible_memory(store, writer, secret, outbox)
    with pytest.raises(SkillApiBlocked, match="not found in the requested scope"):
        get_memory(_profile(store, owner="user-b"), memory_id=memory_id)

    lower_sensitivity = _profile(store).model_copy(update={"max_sensitivity": "internal"})
    with pytest.raises(SkillApiBlocked, match="not found in the requested scope"):
        get_memory(lower_sensitivity, memory_id=memory_id)

    workspace_only = _profile(store).model_copy(update={"allowed_visibilities": ("workspace",)})
    with pytest.raises(SkillApiBlocked, match="not found in the requested scope"):
        get_memory(workspace_only, memory_id=memory_id)


def test_context_pack_includes_current_history_conflicts_and_stays_bounded(
    store, writer, secret, outbox
) -> None:
    add_source(store)
    grant = make_grant(store)
    claim_payload = {"conversation_handoff": {"claim_key": "claim:lighthouse-state"}}
    old_item = make_write_input(
        content="旧灯塔密道原先是开放的。",
        idempotency_key="context-pack-old",
    ).model_copy(update={"domain_payload": claim_payload})
    old = writer.write(old_item, make_token(secret, grant))
    outbox.run_once(owner="context-pack", limit=20)

    new_item = make_write_input(
        content="旧灯塔密道现在已经封闭。",
        idempotency_key="context-pack-new",
        supersedes_id=old.memory_id,
        expected_version=1,
    ).model_copy(update={"domain_payload": claim_payload})
    current = writer.write(new_item, make_token(secret, grant))
    conflict_item = make_write_input(
        content="仍有人主张旧灯塔密道保持开放。",
        idempotency_key="context-pack-conflict",
        evidence_status=EvidenceStatus.UNRESOLVED,
    ).model_copy(update={"domain_payload": claim_payload})
    conflict = writer.write(conflict_item, make_token(secret, grant))
    assert conflict.status.value == "pending_review"
    outbox.run_once(owner="context-pack", limit=20)

    pack = build_context_pack(_profile(store), query="旧灯塔", token_budget=3000)
    assert pack.mode == "context_pack"
    assert pack.estimated_tokens <= 3000
    assert [entry.content for entry in pack.current] == ["旧灯塔密道现在已经封闭。"]
    assert [entry.content for entry in pack.history] == ["旧灯塔密道原先是开放的。"]
    assert [entry.content for entry in pack.conflicts] == ["仍有人主张旧灯塔密道保持开放。"]
    assert pack.source_refs
    assert pack.current[0].claim_key == "claim:lighthouse-state"
    assert "memory_id" not in pack.current[0].model_dump()


def test_context_pack_truncates_large_current_content_to_budget(store, writer, secret, outbox) -> None:
    add_source(store)
    grant = make_grant(store)
    long_content = "旧灯塔" + ("密道状态说明" * 300)
    writer.write(
        make_write_input(content=long_content, idempotency_key="context-pack-long"),
        make_token(secret, grant),
    )
    outbox.run_once(owner="context-pack-budget", limit=20)
    pack = build_context_pack(_profile(store), query="旧灯塔", token_budget=1000)
    assert pack.estimated_tokens <= 1000
    assert pack.truncated is True
    assert pack.current and pack.current[0].content.endswith("…")


def test_health_returns_three_status_dimensions(store, writer, secret, outbox) -> None:
    _write_visible_memory(store, writer, secret, outbox)
    health = health_check(_profile(store))
    assert health.status == "ok"
    assert health.consistency_status == "ok"
    assert health.quality_status == "ok"
    assert health.freshness_status == "current"
    assert health.counts["active"] == 1
    assert health.counts["missing_verified_evidence"] == 0
    assert health.counts["fts_mismatches"] == 0


def test_health_detects_fts_drift(store, writer, secret, outbox) -> None:
    _write_visible_memory(store, writer, secret, outbox)
    with store.transaction() as connection:
        connection.execute("DELETE FROM memory_fts")
    health = health_check(_profile(store))
    assert health.status == "critical"
    assert health.consistency_status == "critical"
    assert health.counts["fts_mismatches"] >= 1

def test_milvus_readonly_query_rechecks_canonical(store, writer, secret, outbox) -> None:
    memory_id = _write_visible_memory(store, writer, secret, outbox)
    profile = _profile(store).model_copy(update={
        "backend": "milvus",
        "read_alias": "memory_read_alias_novel_test_001",
        "physical_collection": "memory_core_novel_test_v1",
        "embedding_model": "BAAI/bge-m3",
        "embedding_dimension": 1024,
        "metric_type": "COSINE",
    })
    read_index = FakeReadIndex(memory_id)
    context = query_context(
        profile,
        query="语义问题",
        read_index=read_index,
        embedding=FakeEmbedding(),
    )
    assert context.backend.value == "milvus"
    assert context.hits
    assert context.hits[0].content.startswith("林岚得知旧灯塔")
    assert 'workspace_id == "ws1"' in read_index.last_filter
    assert 'project_id == "p1"' in read_index.last_filter


def test_milvus_stale_superseded_id_is_rechecked_against_sqlite(store, writer, secret, outbox) -> None:
    old_id = _write_visible_memory(store, writer, secret, outbox)
    grant = make_grant(store)
    replacement = writer.write(
        make_write_input(
            content="林岚确认旧灯塔密道已经封闭。",
            idempotency_key="query-recheck-replacement",
            supersedes_id=old_id,
            expected_version=1,
        ),
        make_token(secret, grant),
    )
    assert replacement.status.value == "active"
    profile = _profile(store).model_copy(update={
        "backend": "milvus",
        "read_alias": "memory_read_alias_novel_test_001",
        "physical_collection": "memory_core_novel_test_v1",
        "embedding_model": "BAAI/bge-m3",
        "embedding_dimension": 1024,
        "metric_type": "COSINE",
    })
    context = query_context(
        profile,
        query="语义问题",
        read_index=FakeReadIndex(old_id),
        embedding=FakeEmbedding(),
    )
    assert context.hits == []


def test_milvus_query_requires_readonly_adapters(store) -> None:
    profile = _profile(store).model_copy(update={
        "backend": "milvus",
        "read_alias": "memory_read_alias_novel_test_001",
        "physical_collection": "memory_core_novel_test_v1",
        "embedding_dimension": 1024,
    })
    with pytest.raises(SkillApiBlocked, match="read-only index"):
        query_context(profile, query="语义问题")

def test_milvus_health_is_readonly_and_reports_not_assessed_consistency(store, writer, secret, outbox) -> None:
    memory_id = _write_visible_memory(store, writer, secret, outbox)
    profile = _profile(store).model_copy(update={
        "backend": "milvus",
        "read_alias": "memory_read_alias_novel_test_001",
        "physical_collection": "memory_core_novel_test_v1",
        "embedding_model": "BAAI/bge-m3",
        "embedding_dimension": 1024,
        "index_schema_version": "memory-index-v1",
        "metric_type": "COSINE",
    })
    health = health_check(profile, read_index=FakeReadIndex(memory_id))
    assert health.status == "warning"
    assert health.consistency_status == "not_assessed"
    assert health.quality_status == "ok"
    assert health.freshness_status == "current"
    assert health.checks["milvus_alias_target_matches"] is True
    assert health.checks["milvus_count_matches"] is True


def test_milvus_health_alias_mismatch_is_critical(store, writer, secret, outbox) -> None:
    memory_id = _write_visible_memory(store, writer, secret, outbox)
    profile = _profile(store).model_copy(update={
        "backend": "milvus",
        "read_alias": "memory_read_alias_novel_test_001",
        "physical_collection": "memory_core_novel_test_v1",
        "embedding_model": "BAAI/bge-m3",
        "embedding_dimension": 1024,
        "index_schema_version": "memory-index-v1",
        "metric_type": "COSINE",
    })
    health = health_check(profile, read_index=FakeReadIndex(memory_id, collection="wrong_collection"))
    assert health.status == "critical"
    assert health.consistency_status == "critical"
    assert health.checks["milvus_alias_target_matches"] is False