from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from memory_core.consolidation import ConsolidationService
from memory_core.db import utc_now_ts
from memory_core.models import ConsolidationInput, SearchRequest, SensitivityLevel
from memory_core.search import HybridSearchGateway

from .conftest import add_source, make_grant, make_token, make_write_input


def test_outbox_event_uniqueness_and_fts_sync_state(store, writer, outbox, secret) -> None:
    add_source(store)
    grant = make_grant(store)
    result = writer.write(make_write_input(), make_token(secret, grant))
    assert len(result.outbox_ids) == 2
    with store.read() as connection:
        first = connection.execute("SELECT * FROM memory_outbox ORDER BY outbox_id LIMIT 1").fetchone()
        inserted = store.insert_outbox(
            outbox_id=first["outbox_id"], memory_id=first["memory_id"],
            workspace_id=first["workspace_id"], target_type=first["target_type"],
            target_name=first["target_name"], operation=first["operation"],
            item_version=first["item_version"], index_schema_version=first["index_schema_version"],
            embedding_model=first["embedding_model"], embedding_dim=first["embedding_dim"],
            connection=connection,
        )
    assert inserted is False
    outbox.run_once(owner="worker", limit=10)
    outbox.run_once(owner="worker", limit=10)
    with store.read() as connection:
        sync = connection.execute("SELECT * FROM memory_fts_sync_state WHERE memory_id=?", (result.memory_id,)).fetchone()
    assert sync["item_version"] == 1
    assert sync["deleted"] == 0


def test_reconcile_uses_physical_target_without_touching_alias_view(store, writer, outbox, secret) -> None:
    add_source(store)
    grant = make_grant(store)
    result = writer.write(make_write_input(content="重建目标事实"), make_token(secret, grant))
    outbox.run_once(owner="worker", limit=10)
    reconcile_ids = outbox.enqueue_reconcile(workspace_id="ws1", target_name="memory_reconcile_v2")
    assert reconcile_ids
    outbox.run_once(owner="reconciler", limit=20)
    docs = outbox.vector_index.collections["memory_reconcile_v2"]
    assert docs[result.memory_id].version == 1
    assert docs[result.memory_id].index_schema_version == "memory-index-v2"
    assert outbox.vector_index.count("memory_read_alias") == 1


def test_payload_filter_whitelist_rejects_unknown_fields(
    store, writer, outbox, token_validator, profiles, embedding, vector_index, secret
) -> None:
    add_source(store)
    grant = make_grant(store)
    writer.write(make_write_input(), make_token(secret, grant))
    outbox.run_once(owner="worker", limit=10)
    gateway = HybridSearchGateway(
        store=store, profiles=profiles, token_validator=token_validator,
        embedding=embedding, vector_index=vector_index,
    )
    with pytest.raises(PermissionError, match="non-whitelisted"):
        gateway.search(
            make_token(secret, grant),
            SearchRequest(query="旧灯塔", payload_filters={"secret_field": "x"}),
        )


def test_consolidation_idempotent_secret_skip_and_ttl_cleanup(store, token_validator) -> None:
    now = utc_now_ts()
    with store.transaction() as connection:
        for wm_id, sensitivity, ttl in (
            ("wm-normal", "private", now + 1000),
            ("wm-secret", "secret", now + 1000),
            ("wm-expired", "private", now - 10),
        ):
            connection.execute(
                """INSERT INTO working_memory(
                   wm_id,workspace_id,project_id,task_id,conversation_id,owner_principal_id,
                   domain_profile,memory_type,subtype,normalized_subject,content,sensitivity,
                   visibility,confidence,ttl_expires_at,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (wm_id, "ws1", "p1", "task-consolidate", "conv1", "user-a", "novel",
                 "fact", "narrative_fact", "", f"working {wm_id}", sensitivity,
                 "private", 0.5, ttl, now),
            )
    service = ConsolidationService(store, token_validator)
    request = ConsolidationInput(
        idempotency_key="consolidate-1", operation_id="operation-1", workspace_id="ws1",
        project_id="p1", task_id="task-consolidate", conversation_id="conv1",
        trigger_source="scheduler",
    )
    first = service.consolidate(request, scheduler_authorized=True)
    second = service.consolidate(request, scheduler_authorized=True)
    assert first == second
    assert first.expired_wm_count == 1
    assert first.skipped_secret_wm_ids == ["wm-secret"]
    assert first.promoted_memory_ids == []

def test_search_gateway_uses_dense_search_ids_with_milvus_filter(
    store, writer, outbox, token_validator, profiles, embedding, secret
) -> None:
    add_source(store)
    grant = make_grant(store)
    writer.write(make_write_input(), make_token(secret, grant))
    outbox.run_once(owner="worker", limit=10)

    class DenseMilvusStub:
        def __init__(self, fake):
            self.fake = fake
            self.last_filter = ""
        def search_ids(self, vector, *, filter, limit):
            self.last_filter = filter
            return [(doc.memory_id, score) for doc, score in self.fake.search(vector, limit=limit)]

    dense = DenseMilvusStub(outbox.vector_index)
    gateway = HybridSearchGateway(
        store=store, profiles=profiles, token_validator=token_validator,
        embedding=embedding, vector_index=dense,
    )
    result = gateway.search(make_token(secret, grant), SearchRequest(query="旧灯塔"))
    assert result.hits
    assert "workspace_id in" in dense.last_filter
    assert "sensitivity in" in dense.last_filter
    assert 'visibility != "private" or owner_principal_id == "user-a"' in dense.last_filter

def test_context_only_retrieval_returns_hits_and_writes_context_audit(
    store, writer, outbox, token_validator, profiles, embedding, vector_index, secret
) -> None:
    add_source(store)
    grant = make_grant(store)
    writer.write(make_write_input(), make_token(secret, grant))
    outbox.run_once(owner="worker", limit=10)
    gateway = HybridSearchGateway(
        store=store, profiles=profiles, token_validator=token_validator,
        embedding=embedding, vector_index=vector_index,
    )
    hits = gateway.retrieve_context(
        make_token(secret, grant), SearchRequest(query="旧灯塔", top_k=5)
    )
    assert hits
    with store.read() as connection:
        audit = connection.execute(
            "SELECT answer_summary,retrieved_ids_json FROM query_audits ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    assert audit["answer_summary"].startswith("[CONTEXT_ONLY]")
    assert json.loads(audit["retrieved_ids_json"]) == [hits[0].memory_id]
