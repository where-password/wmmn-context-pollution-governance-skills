from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


MAX_EFFECTIVE_TO_TS = 9_223_372_036_854_775_807
REDACTED_SECRET_QUERY = "[REDACTED_SECRET_QUERY]"
REDACTED_SECRET_ANSWER = "[REDACTED_SECRET_ANSWER]"
TOKEN_AUDIENCE = "memory-gateway-v1"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MemoryStatus(StrEnum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    REJECTED = "rejected"
    ARCHIVED = "archived"
    PENDING_REVIEW = "pending_review"


class EvidenceStatus(StrEnum):
    VERIFIED = "verified"
    REJECTED = "rejected"
    UNRESOLVED = "unresolved"


class SensitivityLevel(StrEnum):
    PUBLIC = "public"
    INTERNAL = "internal"
    PRIVATE = "private"
    SECRET = "secret"


class DedupPolicy(StrEnum):
    CONTENT_ONLY = "content_only"
    NO_AUTO_MERGE = "no_auto_merge"


class AssertionSource(StrEnum):
    USER_DECISION = "user_decision"
    USER_PREFERENCE = "user_preference"
    USER_REQUEST = "user_request"
    USER_REPORT = "user_report"
    PRIMARY_SOURCE = "primary_source"
    EXTERNAL_DOCUMENT = "external_document"
    TOOL_OUTPUT = "tool_output"
    ASSISTANT_PROPOSAL = "assistant_proposal"
    ASSISTANT_INFERENCE = "assistant_inference"
    LEGACY_UNKNOWN = "legacy_unknown"


class VerificationState(StrEnum):
    NOT_REQUIRED = "not_required"
    QUOTE_VERIFIED = "quote_verified"
    USER_CONFIRMED = "user_confirmed"
    SOURCE_VERIFIED = "source_verified"
    TOOL_VERIFIED = "tool_verified"
    UNVERIFIED = "unverified"
    CONFLICT = "conflict"
    LEGACY_UNCLASSIFIED = "legacy_unclassified"


class OutboxStatus(StrEnum):
    PENDING = "pending"
    CLAIMED = "claimed"
    DONE = "done"
    POISON = "poison"


SENSITIVITY_ORDER: dict[SensitivityLevel, int] = {
    SensitivityLevel.PUBLIC: 0,
    SensitivityLevel.INTERNAL: 1,
    SensitivityLevel.PRIVATE: 2,
    SensitivityLevel.SECRET: 3,
}

MemoryTypeLiteral = Literal[
    "fact",
    "requirement",
    "constraint",
    "decision",
    "task",
    "procedure",
    "issue",
    "solution",
    "artifact",
    "relationship",
    "event",
    "preference",
    "open_question",
]


def max_sensitivity(
    left: SensitivityLevel | str,
    right: SensitivityLevel | str,
) -> SensitivityLevel:
    left_level = SensitivityLevel(left)
    right_level = SensitivityLevel(right)
    return left_level if SENSITIVITY_ORDER[left_level] >= SENSITIVITY_ORDER[right_level] else right_level


class SourceScope(StrictModel):
    scope_type: str
    ref: str
    start: str | int | None = None
    end: str | int | None = None
    locator_json: dict[str, Any] = Field(default_factory=dict)


class MemoryEvidence(StrictModel):
    evidence_id: str
    source_unit_id: str
    quote: str
    locator_json: dict[str, Any] = Field(default_factory=dict)
    status: EvidenceStatus = EvidenceStatus.UNRESOLVED

    @model_validator(mode="after")
    def require_quote(self) -> "MemoryEvidence":
        if not self.quote.strip():
            raise ValueError("evidence quote must not be empty")
        return self


# 1) Client write contract. Security fields are intentionally absent.
class MemoryWriteInput(StrictModel):
    idempotency_key: str
    operation_id: str
    workspace_id: str
    project_id: str
    task_id: str | None = None
    conversation_id: str | None = None
    domain_profile: str
    profile_version: str
    memory_type: MemoryTypeLiteral
    subtype: str | None = None
    subject: str | None = None
    entity_type: str | None = None
    entity_id: str | None = None
    content: str
    importance: Literal["critical", "high", "medium", "low"] = "medium"
    declared_sensitivity: SensitivityLevel = SensitivityLevel.PRIVATE
    requested_visibility: Literal["private", "workspace"] = "private"
    assertion_source: AssertionSource = AssertionSource.USER_REPORT
    verification_state: VerificationState = VerificationState.UNVERIFIED
    effective_from: datetime | None = None
    effective_to: datetime | None = None
    source_id: str
    source_unit_id: str
    source_scope: SourceScope
    confidence: float = Field(ge=0.0, le=1.0, default=0.5)
    supersedes_id: str | None = None
    expected_version: int | None = None
    domain_payload: dict[str, Any] = Field(default_factory=dict)
    evidences: list[MemoryEvidence] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_content_and_time(self) -> "MemoryWriteInput":
        if not self.content.strip():
            raise ValueError("content must not be empty")
        if self.effective_from and self.effective_to and self.effective_to < self.effective_from:
            raise ValueError("effective_to must not precede effective_from")
        return self


# 2) Canonical record.
class MemoryRecord(StrictModel):
    memory_id: str
    workspace_id: str
    project_id: str
    task_id: str | None = None
    conversation_id: str | None = None
    owner_principal_id: str
    domain_profile: str
    profile_version: str
    memory_type: str
    subtype: str | None = None
    subject: str | None = None
    normalized_subject: str = ""
    entity_type: str | None = None
    entity_id: str | None = None
    content: str
    content_hash: str
    content_hash_version: str
    dedup_policy: DedupPolicy
    status: MemoryStatus
    importance: Literal["critical", "high", "medium", "low"]
    sensitivity: SensitivityLevel
    visibility: Literal["private", "workspace", "shared"]
    assertion_source: AssertionSource = AssertionSource.LEGACY_UNKNOWN
    verification_state: VerificationState = VerificationState.LEGACY_UNCLASSIFIED
    effective_from: datetime | None = None
    effective_to: datetime | None = None
    source_id: str
    source_unit_id: str
    source_sha256: str
    source_unit_hash: str
    source_scope: SourceScope
    confidence: float
    supersedes_id: str | None = None
    version: int
    domain_payload: dict[str, Any] = Field(default_factory=dict)
    evidences: list[MemoryEvidence] = Field(default_factory=list)
    deleted_at: datetime | None = None
    delete_reason: str | None = None
    created_at: datetime
    updated_at: datetime


# 3) Derived vector index document.
class MemoryIndexDoc(StrictModel):
    memory_id: str
    embedding: list[float]
    workspace_id: str
    project_id: str
    task_id: str = ""
    conversation_id: str = ""
    owner_principal_id: str
    domain_profile: str
    profile_version: str
    memory_type: str
    subtype: str = ""
    subject: str = ""
    normalized_subject: str = ""
    status: MemoryStatus
    importance: str
    sensitivity: SensitivityLevel
    visibility: str
    effective_from_ts: int = 0
    effective_to_ts: int = MAX_EFFECTIVE_TO_TS
    version: int
    embedding_model: str
    embedding_dim: int
    normalization: Literal["l2", "none"] = "l2"
    index_schema_version: str
    collection_alias: str
    canonical_text: str
    embedding_text: str
    source_scope_json: dict[str, Any]
    indexed_payload_json: dict[str, Any] = Field(default_factory=dict)
    created_at_ts: int


class AccessScope(StrictModel):
    project_ids: list[str] = Field(default_factory=list)
    domains: list[str] = Field(default_factory=list)
    memory_types: list[str] = Field(default_factory=list)
    statuses: list[MemoryStatus] = Field(default_factory=lambda: [MemoryStatus.ACTIVE])
    max_sensitivity: SensitivityLevel = SensitivityLevel.PRIVATE
    allow_global_kb: bool = False
    payload_filters: dict[str, Any] = Field(default_factory=dict)


class AccessGrant(StrictModel):
    grant_id: str
    principal_id: str
    workspace_id: str
    scope: AccessScope
    actions: list[Literal["search", "read", "export", "write", "delete"]] = Field(
        default_factory=lambda: ["search"]
    )
    expires_at: datetime
    remaining_queries: int | None = None
    purpose: str
    revoked: bool = False
    revoked_at: datetime | None = None
    revoked_reason: str | None = None
    created_at: datetime


class AgentContextToken(StrictModel):
    jti: str
    principal_id: str
    task_id: str
    grant_id: str
    workspace_id: str
    audience: Literal["memory-gateway-v1"] = "memory-gateway-v1"
    issued_at: datetime
    expires_at: datetime
    alg: Literal["HMAC-SHA256"] = "HMAC-SHA256"
    signature: str


class SearchRequest(StrictModel):
    query: str
    top_k: int = Field(default=20, ge=1, le=100)
    response_mode: Literal["reader", "professional", "audit"] = "professional"
    project_ids: list[str] = Field(default_factory=list)
    task_id: str | None = None
    conversation_id: str | None = None
    domains: list[str] = Field(default_factory=list)
    memory_types: list[str] = Field(default_factory=list)
    payload_filters: dict[str, Any] = Field(default_factory=dict)
    include_history: bool = False
    include_global_kb: bool = False


class SearchHit(StrictModel):
    memory_id: str
    workspace_id: str
    owner_principal_id: str
    domain_profile: str
    memory_type: str
    subtype: str | None = None
    content: str
    source_scope: SourceScope
    status: MemoryStatus
    importance: str
    sensitivity: SensitivityLevel
    visibility: Literal["private", "workspace", "shared"]
    assertion_source: AssertionSource = AssertionSource.LEGACY_UNKNOWN
    verification_state: VerificationState = VerificationState.LEGACY_UNCLASSIFIED
    origin: Literal["workspace", "global_kb"] = "workspace"
    evidence_id: str | None = None
    score: float


class SynthesisInputItem(StrictModel):
    content: str
    source_label: str
    source_scope: SourceScope
    origin: Literal["workspace", "global_kb"] = "workspace"
    assertion_source: AssertionSource = AssertionSource.LEGACY_UNKNOWN
    verification_state: VerificationState = VerificationState.LEGACY_UNCLASSIFIED


class SynthesisCitation(StrictModel):
    source_label: str
    source_scope: SourceScope
    origin: Literal["workspace", "global_kb"] = "workspace"
    memory_ids: list[str] = Field(default_factory=list)


class SynthesisResult(StrictModel):
    answer: str
    mode: Literal["reader", "professional", "audit"] = "professional"
    citations: list[SynthesisCitation] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    origin: Literal["workspace", "global_kb", "mixed"] = "workspace"
    includes_internal_ids: bool = False


class MemoryDeleteInput(StrictModel):
    idempotency_key: str
    operation_id: str
    workspace_id: str
    target_type: Literal["memory_id", "conversation_id", "workspace_id"]
    target_id: str
    delete_reason: str
    expected_version: int | None = None


class ConsolidationInput(StrictModel):
    idempotency_key: str
    operation_id: str
    workspace_id: str
    project_id: str
    task_id: str
    conversation_id: str | None = None
    trigger_source: Literal["scheduler", "lifecycle_hook", "user_explicit"]


class ConsolidationResult(StrictModel):
    task_id: str
    promoted_memory_ids: list[str] = Field(default_factory=list)
    conflicted_memory_ids: list[str] = Field(default_factory=list)
    skipped_secret_wm_ids: list[str] = Field(default_factory=list)
    expired_wm_count: int = 0


class MemoryWriteResult(StrictModel):
    memory_id: str
    status: MemoryStatus
    version: int
    created: bool
    deduplicated: bool = False
    evidence_ids: list[str] = Field(default_factory=list)
    outbox_ids: list[str] = Field(default_factory=list)


class MemoryDeleteResult(StrictModel):
    deleted_count: int
    memory_ids: list[str] = Field(default_factory=list)
    outbox_ids: list[str] = Field(default_factory=list)


class SearchResult(StrictModel):
    hits: list[SearchHit] = Field(default_factory=list)
    synthesis: SynthesisResult


class QueryAuditRecord(StrictModel):
    query_id: str
    principal_id: str
    token_jti: str
    grant_id: str
    workspace_id: str
    query_hash: str
    redacted_query: str
    max_sensitivity: SensitivityLevel
    filters_json: dict[str, Any] = Field(default_factory=dict)
    retrieved_ids_json: list[str] = Field(default_factory=list)
    answer_summary: str
    created_at: datetime


class AuthorizedContext(StrictModel):
    token: AgentContextToken
    grant: AccessGrant
    effective_scope: AccessScope
    max_sensitivity: SensitivityLevel
    query_budget_remaining: int | None = None
