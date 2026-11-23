from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from memory_core.db import SQLiteStore
from memory_core.fakes import FakeEmbedding, FakeVectorIndex
from memory_core.models import (
    AccessGrant, AccessScope, AgentContextToken, AssertionSource, EvidenceStatus,
    MemoryEvidence, MemoryWriteInput, SensitivityLevel, SourceScope, VerificationState,
)
from memory_core.outbox import OutboxWorker
from memory_core.profiles import ProfileRegistry
from memory_core.security import TokenValidator, issue_token
from memory_core.writer import MemoryWriter


@pytest.fixture
def secret() -> bytes:
    return b"test-only-hmac-secret"


@pytest.fixture
def profiles() -> ProfileRegistry:
    return ProfileRegistry()


@pytest.fixture
def store(tmp_path: Path) -> SQLiteStore:
    value = SQLiteStore(tmp_path / "memory.sqlite3")
    value.migrate()
    value.ensure_workspace_project(
        workspace_id="ws1", workspace_name="Workspace 1",
        project_id="p1", project_name="Project 1",
    )
    return value


@pytest.fixture
def embedding() -> FakeEmbedding:
    return FakeEmbedding(dimension=1024)


@pytest.fixture
def vector_index() -> FakeVectorIndex:
    return FakeVectorIndex()


@pytest.fixture
def token_validator(store: SQLiteStore, secret: bytes) -> TokenValidator:
    return TokenValidator(store, secret=secret)


@pytest.fixture
def writer(store: SQLiteStore, profiles: ProfileRegistry, token_validator: TokenValidator) -> MemoryWriter:
    return MemoryWriter(store=store, profiles=profiles, token_validator=token_validator)


@pytest.fixture
def outbox(store: SQLiteStore, profiles: ProfileRegistry, embedding: FakeEmbedding, vector_index: FakeVectorIndex) -> OutboxWorker:
    return OutboxWorker(store=store, profiles=profiles, embedding=embedding, vector_index=vector_index)


def make_grant(
    store: SQLiteStore, *, principal_id: str = "user-a", grant_id: str | None = None,
    actions: list[str] | None = None, max_sensitivity: SensitivityLevel = SensitivityLevel.PRIVATE,
    remaining_queries: int | None = None, project_ids: list[str] | None = None,
    domains: list[str] | None = None, expires_in: int = 3600,
) -> AccessGrant:
    now = datetime.now(timezone.utc)
    grant = AccessGrant(
        grant_id=grant_id or f"grant-{uuid.uuid4().hex}", principal_id=principal_id,
        workspace_id="ws1",
        scope=AccessScope(
            project_ids=project_ids if project_ids is not None else ["p1"],
            domains=domains if domains is not None else ["novel", "generic"],
            max_sensitivity=max_sensitivity, allow_global_kb=False,
        ),
        actions=actions or ["write", "search", "delete"],
        expires_at=now + timedelta(seconds=expires_in), remaining_queries=remaining_queries,
        purpose="test", created_at=now,
    )
    store.save_grant(grant)
    return grant


def make_token(secret: bytes, grant: AccessGrant, *, ttl_seconds: int = 300) -> AgentContextToken:
    return issue_token(
        secret=secret, principal_id=grant.principal_id, task_id="task-1",
        grant_id=grant.grant_id, workspace_id=grant.workspace_id, ttl_seconds=ttl_seconds,
    )


def add_source(
    store: SQLiteStore, *, source_id: str = "src1", source_unit_id: str = "unit1",
    visibility: str = "workspace", owner_principal_id: str | None = None,
    sensitivity: SensitivityLevel = SensitivityLevel.PRIVATE,
    text: str = "林岚在旧灯塔发现密道。", scope_ref: str = "chapter-1",
    source_type: str = "novel_turn",
) -> None:
    store.insert_source(
        source_id=source_id, workspace_id="ws1", project_id="p1",
        visibility=visibility, owner_principal_id=owner_principal_id,
        source_path=f"memory://{source_id}", source_sha256=hashlib.sha256(text.encode()).hexdigest(),
        source_type=source_type, sensitivity=sensitivity.value,
    )
    store.insert_source_unit(
        source_unit_id=source_unit_id, source_id=source_id, parent_unit_id=None,
        scope_type="chat_turn", scope_ref=scope_ref,
        source_unit_hash=hashlib.sha256(text.encode()).hexdigest(),
        text_content=text, metadata={},
    )


def make_write_input(
    *, source_id: str = "src1", source_unit_id: str = "unit1",
    content: str = "林岚得知旧灯塔密道通往北堤。", subject: str | None = "林岚",
    memory_type: str = "fact", subtype: str | None = "narrative_fact",
    visibility: str = "private", sensitivity: SensitivityLevel = SensitivityLevel.PRIVATE,
    evidence_status: EvidenceStatus = EvidenceStatus.VERIFIED,
    idempotency_key: str | None = None, supersedes_id: str | None = None,
    expected_version: int | None = None,
    domain_profile: str = "novel", profile_version: str = "1.0.0",
    assertion_source: AssertionSource | None = None,
    verification_state: VerificationState | None = None,
) -> MemoryWriteInput:
    if assertion_source is None:
        assertion_source = (
            AssertionSource.PRIMARY_SOURCE if domain_profile == "novel"
            else AssertionSource.USER_REPORT
        )
    if verification_state is None:
        verification_state = (
            VerificationState.SOURCE_VERIFIED if domain_profile == "novel"
            else VerificationState.UNVERIFIED
        )
    return MemoryWriteInput(
        idempotency_key=idempotency_key or f"idem-{uuid.uuid4().hex}",
        operation_id=f"op-{uuid.uuid4().hex}", workspace_id="ws1", project_id="p1",
        domain_profile=domain_profile, profile_version=profile_version, memory_type=memory_type,
        subtype=subtype, subject=subject, entity_type="character", content=content,
        importance="high", declared_sensitivity=sensitivity,
        requested_visibility=visibility, source_id=source_id, source_unit_id=source_unit_id,
        assertion_source=assertion_source, verification_state=verification_state,
        source_scope=SourceScope(scope_type="chat_turn", ref="chapter-1"),
        confidence=0.9, supersedes_id=supersedes_id, expected_version=expected_version,
        domain_payload={
            "memory_category": "character_state",
            "retention_reason": "该信息会影响后续探索行动。", "chapter_index": 1,
        },
        evidences=[MemoryEvidence(
            evidence_id=f"ev-{uuid.uuid4().hex}", source_unit_id=source_unit_id,
            quote="林岚在旧灯塔发现密道。", status=evidence_status,
        )],
    )
