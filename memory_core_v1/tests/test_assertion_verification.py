from __future__ import annotations

import pytest

from memory_core.db import SQLiteStore
from memory_core.models import (
    AssertionSource,
    MemoryStatus,
    SearchRequest,
    VerificationState,
)
from memory_core.search import HybridSearchGateway

from .conftest import add_source, make_grant, make_token, make_write_input


def _write(store, writer, secret, **kwargs):
    add_source(store)
    grant = make_grant(store, principal_id="assertion-user")
    return writer.write(make_write_input(**kwargs), make_token(secret, grant))


def test_assistant_inference_without_strong_verification_cannot_be_active(store, writer, secret) -> None:
    with pytest.raises(ValueError, match="assistant_inference"):
        _write(
            store, writer, secret, content="未经验证的助手推断。",
            domain_profile="generic", subtype=None,
            assertion_source=AssertionSource.ASSISTANT_INFERENCE,
            verification_state=VerificationState.UNVERIFIED,
        )


def test_assistant_inference_with_tool_verification_can_be_active(store, writer, secret) -> None:
    result = _write(
        store, writer, secret, content="工具验证过的助手推断。",
        domain_profile="generic", subtype=None,
        assertion_source=AssertionSource.ASSISTANT_INFERENCE,
        verification_state=VerificationState.TOOL_VERIFIED,
    )
    assert result.status == MemoryStatus.ACTIVE
    record = store.get_memory_record(result.memory_id)
    assert record.assertion_source == AssertionSource.ASSISTANT_INFERENCE
    assert record.verification_state == VerificationState.TOOL_VERIFIED


def test_explicit_user_reclassification_to_decision_can_be_active(store, writer, secret) -> None:
    result = _write(
        store, writer, secret, content="用户明确改判后的决定。",
        memory_type="decision", subtype=None, domain_profile="generic",
        assertion_source=AssertionSource.USER_DECISION,
        verification_state=VerificationState.USER_CONFIRMED,
    )
    assert result.status == MemoryStatus.ACTIVE
    record = store.get_memory_record(result.memory_id)
    assert record.assertion_source == AssertionSource.USER_DECISION
    assert record.verification_state == VerificationState.USER_CONFIRMED


def test_user_preference_needs_no_external_fact_verification(store, writer, secret) -> None:
    result = _write(
        store, writer, secret, content="这是用户的项目格式偏好。",
        memory_type="preference", subtype=None, domain_profile="generic",
        assertion_source=AssertionSource.USER_PREFERENCE,
        verification_state=VerificationState.NOT_REQUIRED,
    )
    assert result.status == MemoryStatus.ACTIVE


def test_user_report_objective_fact_stays_unverified_in_search_and_answer(
    store, writer, outbox, token_validator, profiles, embedding, vector_index, secret
) -> None:
    add_source(store)
    grant = make_grant(store, principal_id="assertion-user")
    result = writer.write(
        make_write_input(
            content="用户报告旧灯塔密道通往北堤。",
            domain_profile="generic", subtype=None,
            assertion_source=AssertionSource.USER_REPORT,
            verification_state=VerificationState.UNVERIFIED,
        ),
        make_token(secret, grant),
    )
    assert result.status == MemoryStatus.ACTIVE
    outbox.run_once(owner="assertion-indexer", limit=10)
    gateway = HybridSearchGateway(
        store=store, profiles=profiles, token_validator=token_validator,
        embedding=embedding, vector_index=vector_index,
    )
    response = gateway.search(
        make_token(secret, grant),
        SearchRequest(query="旧灯塔", top_k=5, response_mode="reader"),
    )
    assert response.hits
    hit = response.hits[0]
    assert hit.assertion_source == AssertionSource.USER_REPORT
    assert hit.verification_state == VerificationState.UNVERIFIED
    assert "用户陈述（未外部验证）" in response.synthesis.answer
    assert response.synthesis.uncertainties


def test_primary_source_novel_fact_uses_source_verification(store, writer, secret) -> None:
    result = _write(store, writer, secret, content="林岚在灯塔发现密道。")
    record = store.get_memory_record(result.memory_id)
    assert result.status == MemoryStatus.ACTIVE
    assert record.assertion_source == AssertionSource.PRIMARY_SOURCE
    assert record.verification_state == VerificationState.SOURCE_VERIFIED


def test_migration_is_idempotent_and_adds_dual_columns(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "migration.sqlite3")
    store.migrate()
    store.migrate()
    with store.read() as connection:
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(memory_items)")}
        migrations = {row["migration_id"] for row in connection.execute("SELECT migration_id FROM schema_migrations")}
    assert {"assertion_source", "verification_state"} <= columns
    assert migrations == {"001_core", "002_assertion_verification", "003_incremental_ingestion_raw", "004_candidate_extraction", "005_step3_retry_audit"}
