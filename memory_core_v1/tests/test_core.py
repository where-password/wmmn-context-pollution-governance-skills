from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from memory_core.db import utc_now_ts
from memory_core.delete import MemoryDeleteService
from memory_core.models import (
    AgentContextToken,
    EvidenceStatus,
    MemoryDeleteInput,
    MemoryStatus,
    SearchRequest,
    SensitivityLevel,
)
from memory_core.search import HybridSearchGateway
from memory_core.security import sign_token
from memory_core.writer import IdempotencyConflict, SourceAccessDenied

from .conftest import add_source, make_grant, make_token, make_write_input


def _write_with_fresh_token(writer, item, secret, grant):
    return writer.write(item, make_token(secret, grant))


def test_active_requires_verified_evidence(store, writer, secret) -> None:
    add_source(store)
    grant = make_grant(store)
    active = _write_with_fresh_token(writer, make_write_input(), secret, grant)
    assert active.status == MemoryStatus.ACTIVE
    assert store.get_memory_record(active.memory_id).evidences[0].status == EvidenceStatus.VERIFIED

    pending = _write_with_fresh_token(
        writer,
        make_write_input(content="林岚尚未确认密道是否安全。", evidence_status=EvidenceStatus.UNRESOLVED),
        secret,
        grant,
    )
    assert pending.status == MemoryStatus.PENDING_REVIEW
    assert store.get_memory_record(pending.memory_id).status == MemoryStatus.PENDING_REVIEW


def test_private_dedup_is_owner_scoped(store, writer, secret) -> None:
    add_source(store)
    grant_a = make_grant(store, principal_id="owner-a")
    first = _write_with_fresh_token(writer, make_write_input(content="同一私有事实"), secret, grant_a)
    second = _write_with_fresh_token(writer, make_write_input(content="同一私有事实"), secret, grant_a)
    assert second.deduplicated is True
    assert second.memory_id == first.memory_id

    grant_b = make_grant(store, principal_id="owner-b")
    other = _write_with_fresh_token(writer, make_write_input(content="同一私有事实"), secret, grant_b)
    assert other.deduplicated is False
    assert other.memory_id != first.memory_id


def test_workspace_dedup_is_shared_across_owners(store, writer, secret) -> None:
    add_source(store)
    grant_a = make_grant(store, principal_id="owner-a")
    grant_b = make_grant(store, principal_id="owner-b")
    first = _write_with_fresh_token(
        writer, make_write_input(content="共享工作空间事实", visibility="workspace"), secret, grant_a
    )
    second = _write_with_fresh_token(
        writer, make_write_input(content="共享工作空间事实", visibility="workspace"), secret, grant_b
    )
    assert second.deduplicated is True
    assert second.memory_id == first.memory_id


def test_private_source_forces_private_owner_and_visibility(store, writer, secret) -> None:
    add_source(store, visibility="private", owner_principal_id="owner-a")
    grant = make_grant(store, principal_id="owner-a")
    result = _write_with_fresh_token(
        writer, make_write_input(content="私有来源派生事实", visibility="workspace"), secret, grant
    )
    record = store.get_memory_record(result.memory_id)
    assert record.owner_principal_id == "owner-a"
    assert record.visibility == "private"

    grant_b = make_grant(store, principal_id="owner-b")
    with pytest.raises(SourceAccessDenied):
        _write_with_fresh_token(
            writer, make_write_input(content="越权私有来源事实", visibility="workspace"), secret, grant_b
        )


def test_security_fields_are_rejected_from_write_contract() -> None:
    base = make_write_input().model_dump(mode="json")
    for field, value in (
        ("owner_principal_id", "forged"),
        ("content_hash", "forged"),
        ("source_unit_hash", "forged"),
        ("dedup_policy", "no_auto_merge"),
    ):
        payload = dict(base)
        payload[field] = value
        with pytest.raises(ValidationError):
            type(make_write_input()).model_validate(payload)


def test_outbox_fts_trigram_and_two_character_subject(store, writer, outbox, secret) -> None:
    add_source(store)
    grant = make_grant(store)
    result = _write_with_fresh_token(writer, make_write_input(), secret, grant)
    counts = outbox.run_once(owner="worker-1", limit=10)
    assert counts
    assert store.fts_search("旧灯塔")[0]["memory_id"] == result.memory_id
    assert store.subject_search("林岚")[0]["memory_id"] == result.memory_id
    assert outbox.run_once(owner="worker-1", limit=10) == {}


def test_outbox_claim_lease_poison_and_manual_resume(store, writer, secret) -> None:
    add_source(store)
    grant = make_grant(store)
    result = _write_with_fresh_token(writer, make_write_input(), secret, grant)
    base = utc_now_ts()
    claimed = store.claim_outbox(owner="worker-1", limit=10, lease_seconds=1, now=base)
    assert len(claimed) == 2
    assert all(event["status"] == "claimed" for event in claimed)
    reclaim = store.claim_outbox(owner="worker-2", limit=10, lease_seconds=60, now=base + 2)
    assert len(reclaim) == 2
    for index in range(6):
        status = store.fail_outbox(reclaim[0]["outbox_id"], error="boom", max_retries=5, now=base + 10 + index)
    assert status == "poison"
    store.resume_poison_outbox(reclaim[0]["outbox_id"], now=base + 20)
    assert store.get_outbox(reclaim[0]["outbox_id"])["status"] == "pending"


def test_stale_outbox_upsert_cannot_overwrite_newer_version(store, writer, outbox, secret) -> None:
    add_source(store)
    grant = make_grant(store)
    first = _write_with_fresh_token(writer, make_write_input(content="旧事实"), secret, grant)
    old_events = store.claim_outbox(owner="old-worker", limit=20, lease_seconds=60)
    second = _write_with_fresh_token(
        writer,
        make_write_input(content="新事实", supersedes_id=first.memory_id, expected_version=1),
        secret,
        grant,
    )
    new_events = store.claim_outbox(owner="new-worker", limit=20, lease_seconds=60)
    for event in old_events:
        outbox.process_event(event)
    for event in new_events:
        outbox.process_event(event)
    docs = outbox.vector_index.collections[outbox.vector_index.read_alias]
    assert docs[second.memory_id].version == 2
    assert docs[second.memory_id].canonical_text == "新事实"


def test_token_signature_ttl_replay_and_query_budget(store, token_validator, secret) -> None:
    grant = make_grant(store, remaining_queries=1)
    token = make_token(secret, grant)
    token_validator.validate(token)
    with pytest.raises(PermissionError, match="replay"):
        token_validator.validate(token)
    second = make_token(secret, grant)
    with pytest.raises(PermissionError, match="budget"):
        token_validator.validate(second)

    expired_grant = make_grant(store, principal_id="expired-user")
    expired = make_token(secret, expired_grant, ttl_seconds=-60)
    with pytest.raises(PermissionError, match="expired"):
        token_validator.validate(expired)

    forged = token.model_copy(update={"signature": "0" * 64})
    with pytest.raises(PermissionError, match="signature"):
        token_validator.validate(forged)


def test_search_grant_intersection_reader_audit_and_secret_redaction(
    store, writer, outbox, token_validator, profiles, embedding, vector_index, secret
) -> None:
    add_source(store)
    grant = make_grant(store, max_sensitivity=SensitivityLevel.SECRET)
    result = _write_with_fresh_token(writer, make_write_input(), secret, grant)
    outbox.run_once(owner="indexer", limit=10)
    gateway = HybridSearchGateway(
        store=store, profiles=profiles, token_validator=token_validator,
        embedding=embedding, vector_index=vector_index,
    )
    reader = gateway.search(
        make_token(secret, grant),
        SearchRequest(query="旧灯塔", top_k=5, response_mode="reader"),
    )
    assert reader.hits and reader.hits[0].memory_id == result.memory_id
    assert reader.synthesis.includes_internal_ids is False
    assert all(not citation.memory_ids for citation in reader.synthesis.citations)

    audit = gateway.search(
        make_token(secret, grant),
        SearchRequest(query="旧灯塔", top_k=5, response_mode="audit"),
    )
    assert audit.synthesis.includes_internal_ids is True
    assert audit.synthesis.citations[0].memory_ids == [result.memory_id]

    outside = gateway.search(
        make_token(secret, grant),
        SearchRequest(query="旧灯塔", project_ids=["p2"], top_k=5),
    )
    assert outside.hits == []

    with store.read() as connection:
        audit_row = connection.execute("SELECT * FROM query_audits ORDER BY created_at DESC LIMIT 1").fetchone()
    assert audit_row["redacted_query"] == "[REDACTED_SECRET_QUERY]"
    assert audit_row["answer_summary"] == "[REDACTED_SECRET_ANSWER]"


def test_delete_is_idempotent_tombstones_and_removes_search_hit(
    store, writer, outbox, token_validator, profiles, embedding, vector_index, secret
) -> None:
    add_source(store)
    grant = make_grant(store)
    result = _write_with_fresh_token(writer, make_write_input(), secret, grant)
    outbox.run_once(owner="indexer", limit=10)
    delete_service = MemoryDeleteService(store=store, token_validator=token_validator)
    request = MemoryDeleteInput(
        idempotency_key="delete-1", operation_id="delete-op-1", workspace_id="ws1",
        target_type="memory_id", target_id=result.memory_id, delete_reason="test delete",
        expected_version=1,
    )
    deleted = delete_service.delete(request, make_token(secret, grant))
    assert deleted.deleted_count == 1
    again = delete_service.delete(request, make_token(secret, grant))
    assert again == deleted
    outbox.run_once(owner="indexer", limit=20)
    record = store.get_memory_record(result.memory_id)
    assert record.deleted_at is not None
    assert store.fts_search("旧灯塔") == []
    gateway = HybridSearchGateway(
        store=store, profiles=profiles, token_validator=token_validator,
        embedding=embedding, vector_index=vector_index,
    )
    search = gateway.search(make_token(secret, grant), SearchRequest(query="旧灯塔"))
    assert search.hits == []


def test_short_chinese_query_uses_restricted_substring_fallback(
    store, writer, outbox, token_validator, profiles, embedding, vector_index, secret
) -> None:
    add_source(store, source_id="src-inspection", source_unit_id="unit-inspection", text="设备巡检")
    grant = make_grant(store)
    result = _write_with_fresh_token(
        writer,
        make_write_input(
            source_id="src-inspection", source_unit_id="unit-inspection",
            content="设备巡检", memory_type="fact", subject="设备",
        ),
        secret,
        grant,
    )
    outbox.run_once(owner="short-query-indexer", limit=10)
    gateway = HybridSearchGateway(
        store=store, profiles=profiles, token_validator=token_validator,
        embedding=embedding, vector_index=vector_index,
    )
    token = make_token(secret, grant)
    response = gateway.search(token, SearchRequest(query="巡检", top_k=5))
    assert any(hit.memory_id == result.memory_id for hit in response.hits)

    assert gateway.search(
        make_token(secret, grant), SearchRequest(query="巡检", project_ids=["p2"], top_k=5)
    ).hits == []
    assert gateway.search(
        make_token(secret, grant), SearchRequest(query="巡检", domains=["generic"], top_k=5)
    ).hits == []

    restricted = store.substring_search(
        "巡检", workspace_ids=("ws1",), project_ids=("p2",),
        domains=("novel",), statuses=(result.status,),
        max_sensitivity=SensitivityLevel.PRIVATE,
        principal_id=grant.principal_id,
    )
    assert restricted == []
