from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import uuid
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from ..db import MIGRATIONS_DIR, SQLiteStore, json_loads
from ..delete import MemoryDeleteService
from ..models import (
    AccessGrant,
    AccessScope,
    AgentContextToken,
    MemoryDeleteInput,
    MemoryDeleteResult,
    MemoryWriteInput,
    MemoryWriteResult,
    SensitivityLevel,
)
from ..profiles import ProfileRegistry
from ..security import TokenValidator, issue_token, sign_token
from ..writer import MemoryWriter
from .models import SkillApiBlocked

HANDOFF_CONTRACT_VERSION = "conversation-memory-handoff-v1"
APPROVAL_CONTRACT_VERSION = "conversation-memory-handoff-approval-v1"
HANDOFF_INGEST_PLAN_VERSION = "skill-handoff-ingest-plan-v1"
HANDOFF_INGEST_APPLY_VERSION = "skill-handoff-ingest-apply-v1"
HANDOFF_INGEST_LOCAL_APPLY_VERSION = "skill-handoff-ingest-local-apply-v1"
HANDOFF_DATA_ARTIFACTS = (
    "memory_write_batch.jsonl",
    "source_units.jsonl",
    "state_transitions.jsonl",
    "review_pending.jsonl",
    "HANDOFF_REVIEW.md",
)
APPROVAL_MANIFEST = "approval_manifest.json"
APPROVAL_LEDGER = "approval_ledger.jsonl"
APPROVED_BATCH = "approved_memory_write_batch.jsonl"
HANDOFF_MANIFEST = "handoff_manifest.json"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_APPROVAL_ID_RE = re.compile(r"^hap_[0-9a-f]{32}$")


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class HandoffSourceScope(_StrictModel):
    scope_type: str
    ref: str
    start: str | int | None = None
    end: str | int | None = None
    locator_json: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def nonempty(self) -> "HandoffSourceScope":
        if not self.scope_type.strip() or not self.ref.strip():
            raise ValueError("source_scope type/ref must be non-empty")
        return self


class HandoffEvidence(_StrictModel):
    evidence_id: str
    source_unit_id: str
    quote: str
    locator_json: dict[str, Any] = Field(default_factory=dict)
    status: Literal["verified", "rejected", "unresolved"] = "unresolved"

    @model_validator(mode="after")
    def nonempty(self) -> "HandoffEvidence":
        if not self.evidence_id.strip() or not self.source_unit_id.strip() or not self.quote.strip():
            raise ValueError("evidence identity fields must be non-empty")
        return self


_SOURCE_UNIT_METADATA_IDENTITY_FIELDS = frozenset({
    "parent_unit_id",
    "conversation_id",
    "message_id",
    "role",
    "sequence",
    "timestamp",
})
_SOURCE_UNIT_METADATA_RESERVED = frozenset({
    "workspace_id",
    "project_id",
    "source_id",
    "source_unit_hash",
    "source_type",
    "source_path",
    "source_sha256",
    "visibility",
    "sensitivity",
})


class HandoffSourceUnit(_StrictModel):
    source_id: str
    workspace_id: str
    project_id: str
    source_unit_id: str
    parent_unit_id: str | None = None
    source_unit_hash: str
    scope_type: str
    scope_ref: str
    text_content: str
    conversation_id: str | None
    message_id: str
    role: str
    sequence: int = Field(ge=0)
    timestamp: str
    source_type: str
    source_path: str
    source_sha256: str
    visibility: Literal["workspace", "private"]
    sensitivity: SensitivityLevel
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("source_unit_hash", "source_sha256")
    @classmethod
    def sha256_format(cls, value: str) -> str:
        if not _SHA256_RE.fullmatch(value):
            raise ValueError("source hash must be a lowercase SHA-256 digest")
        return value

    @field_validator("timestamp")
    @classmethod
    def timestamp_aware(cls, value: str) -> str:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("source unit timestamp must include a timezone offset")
        return value

    @model_validator(mode="after")
    def validate_source_unit(self) -> "HandoffSourceUnit":
        for name in (
            "source_id",
            "workspace_id",
            "project_id",
            "source_unit_id",
            "scope_type",
            "scope_ref",
            "text_content",
            "message_id",
            "role",
            "source_type",
            "source_path",
        ):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must be non-empty")
        if self.parent_unit_id is not None and not self.parent_unit_id.strip():
            raise ValueError("parent_unit_id must be null or a non-empty string")
        reserved = set(self.metadata) & _SOURCE_UNIT_METADATA_RESERVED
        if reserved:
            raise ValueError(f"source unit metadata contains reserved keys: {sorted(reserved)}")
        for name in _SOURCE_UNIT_METADATA_IDENTITY_FIELDS:
            if name in self.metadata and self.metadata[name] != getattr(self, name):
                raise ValueError(f"source unit metadata {name} conflicts with top-level field")
        return self

    def registration_metadata(self) -> dict[str, Any]:
        metadata = dict(self.metadata)
        metadata.update({
            "workspace_id": self.workspace_id,
            "project_id": self.project_id,
            "source_id": self.source_id,
            "parent_unit_id": self.parent_unit_id,
            "conversation_id": self.conversation_id,
            "message_id": self.message_id,
            "role": self.role,
            "sequence": self.sequence,
            "timestamp": self.timestamp,
            "source_type": self.source_type,
            "source_path": self.source_path,
            "source_sha256": self.source_sha256,
            "visibility": self.visibility,
            "sensitivity": self.sensitivity.value,
        })
        return metadata


class HandoffItem(_StrictModel):
    idempotency_key: str
    operation_id: str
    workspace_id: str
    project_id: str
    task_id: str | None = None
    conversation_id: str | None = None
    domain_profile: str
    profile_version: str
    memory_type: str
    subtype: str | None = None
    subject: str | None = None
    entity_type: str | None = None
    entity_id: str | None = None
    content: str
    importance: str = "medium"
    declared_sensitivity: str = "private"
    requested_visibility: str = "private"
    assertion_source: str = "user_report"
    verification_state: str = "unverified"
    effective_from: str | None = None
    effective_to: str | None = None
    source_id: str
    source_unit_id: str
    source_scope: HandoffSourceScope
    confidence: float = Field(ge=0.0, le=1.0)
    supersedes_ref: str | None = None
    supersedes_id: str | None = None
    expected_version: int | None = None
    domain_payload: dict[str, Any] = Field(default_factory=dict)
    evidences: tuple[HandoffEvidence, ...] = ()
    claim_key: str
    scope_key: str
    observed_at: str
    transition_kind: str

    @field_validator("observed_at")
    @classmethod
    def observed_at_aware(cls, value: str) -> str:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("observed_at must include a timezone offset")
        return value

    @field_validator("effective_from", "effective_to")
    @classmethod
    def optional_time_aware(cls, value: str | None) -> str | None:
        if value is None:
            return value
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("effective timestamps must include a timezone offset")
        return value

    @model_validator(mode="after")
    def validate_item(self) -> "HandoffItem":
        for name in ("idempotency_key", "operation_id", "workspace_id", "project_id", "domain_profile", "profile_version", "memory_type", "content", "source_id", "source_unit_id", "claim_key", "scope_key"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must be non-empty")
        if self.expected_version is not None and self.expected_version < 0:
            raise ValueError("expected_version must be non-negative")
        if self.transition_kind == "supersedes" and not (self.supersedes_ref or self.supersedes_id):
            raise ValueError("supersedes transition requires supersedes_ref or supersedes_id")
        if "conversation_handoff" in self.domain_payload:
            raise ValueError("domain_payload must not contain reserved conversation_handoff")
        return self

    def canonical_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class HandoffManifest(_StrictModel):
    handoff_contract: str
    handoff_contract_version: str
    handoff_id: str
    created_at: str
    run_id: str
    plan_id: str
    source_sha256: str
    observations_sha256: str
    workspace_id: str
    project_id: str
    domain_profile: str
    profile_version: str
    source_id: str
    conversation_ids: tuple[str, ...]
    counts: dict[str, int]
    artifacts: dict[str, str]
    approval_manifest_sha256: str | None = None
    review_only: bool = True
    production_writes: bool = False
    canonical_writes: bool = False
    real_llm_called: bool = False
    milvus_written: bool = False
    alias_changed: bool = False
    contains_security_fields: bool = False

    @model_validator(mode="after")
    def validate_manifest(self) -> "HandoffManifest":
        if self.handoff_contract != "conversation-memory-handoff":
            raise ValueError("handoff manifest contract is unsupported")
        if self.handoff_contract_version != HANDOFF_CONTRACT_VERSION:
            raise ValueError("handoff manifest contract version is unsupported")
        if self.review_only is not True:
            raise ValueError("handoff manifest review_only must be true")
        for name in ("production_writes", "canonical_writes", "real_llm_called", "milvus_written", "alias_changed", "contains_security_fields"):
            if getattr(self, name) is not False:
                raise ValueError(f"handoff manifest safety flag must be false: {name}")
        if set(self.artifacts) != set(HANDOFF_DATA_ARTIFACTS):
            raise ValueError("handoff manifest must hash exactly the five data artifacts")
        for name, digest in self.artifacts.items():
            if not _SHA256_RE.fullmatch(digest):
                raise ValueError(f"invalid artifact hash: {name}")
        if not self.conversation_ids:
            raise ValueError("handoff manifest conversation_ids must not be empty")
        return self

class HandoffApprovalManifest(_StrictModel):
    approval_contract_version: str
    handoff_id: str
    ledger_sha256: str
    approved_batch_sha256: str
    item_count: int = Field(ge=0)
    approved_count: int = Field(ge=0)
    rejected_count: int = Field(ge=0)
    deferred_count: int = Field(ge=0)
    revoked_count: int = Field(ge=0)
    pending_count: int = Field(ge=0)
    production_writes: bool = False
    real_llm_called: bool = False
    milvus_written: bool = False
    alias_changed: bool = False

    @model_validator(mode="after")
    def validate_manifest(self) -> "HandoffApprovalManifest":
        if self.approval_contract_version != APPROVAL_CONTRACT_VERSION:
            raise ValueError("approval manifest contract version is unsupported")
        for name in ("production_writes", "real_llm_called", "milvus_written", "alias_changed"):
            if getattr(self, name) is not False:
                raise ValueError(f"approval manifest safety flag must be false: {name}")
        if not _SHA256_RE.fullmatch(self.ledger_sha256) or not _SHA256_RE.fullmatch(self.approved_batch_sha256):
            raise ValueError("approval manifest hashes are invalid")
        return self


class HandoffApprovalRecord(_StrictModel):
    approval_contract_version: str
    approval_id: str
    handoff_id: str
    item_idempotency_key: str
    item_sha256: str
    decision: Literal["approve", "reject", "defer", "revoke"]
    reviewer_id: str
    reason: str
    reviewed_at: str

    @model_validator(mode="after")
    def validate_approval(self) -> "HandoffApprovalRecord":
        if self.approval_contract_version != APPROVAL_CONTRACT_VERSION:
            raise ValueError("approval contract version is unsupported")
        if not _APPROVAL_ID_RE.fullmatch(self.approval_id):
            raise ValueError("approval_id has invalid format")
        if not _SHA256_RE.fullmatch(self.item_sha256):
            raise ValueError("item_sha256 has invalid format")
        if not all(getattr(self, name).strip() for name in ("handoff_id", "item_idempotency_key", "reviewer_id", "reason")):
            raise ValueError("approval text fields must be non-empty")
        parsed = datetime.fromisoformat(self.reviewed_at.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("reviewed_at must include a timezone offset")
        material = {
            "approval_contract_version": APPROVAL_CONTRACT_VERSION,
            "handoff_id": self.handoff_id,
            "item_idempotency_key": self.item_idempotency_key,
            "item_sha256": self.item_sha256,
            "decision": self.decision,
            "reviewer_id": self.reviewer_id,
            "reason": self.reason,
            "reviewed_at": self.reviewed_at,
        }
        expected = "hap_" + hashlib.sha256(_canonical_json(material).encode("utf-8")).hexdigest()[:32]
        if self.approval_id != expected:
            raise ValueError("approval_id does not match approval fields")
        return self


class WrittenMemoryRef(_StrictModel):
    memory_id: str
    version: int = Field(ge=1)
    request_sha256: str | None = None

    @model_validator(mode="after")
    def validate_ref(self) -> "WrittenMemoryRef":
        if not self.memory_id.strip():
            raise ValueError("written_memory_map.memory_id must be non-empty")
        if self.request_sha256 is not None and not _SHA256_RE.fullmatch(self.request_sha256):
            raise ValueError("written_memory_map.request_sha256 must be a lowercase SHA-256")
        return self


class BlockedHandoffItem(_StrictModel):
    idempotency_key: str
    decision: str
    reason: str
    approval_id: str | None = None
    item_sha256: str | None = None


class PendingPostWriteRevocation(_StrictModel):
    idempotency_key: str
    approval_id: str
    item_sha256: str
    reason: str


class AlreadyAppliedItem(_StrictModel):
    idempotency_key: str
    approval_id: str
    memory_id: str
    version: int


class HandoffIngestCounts(_StrictModel):
    items: int
    writes: int
    deletes: int
    already_applied: int
    blocked_items: int
    pending_post_write_revocations: int


class HandoffSourceRegistrationCounts(_StrictModel):
    sources: int
    source_units: int


class HandoffIngestPlan(_StrictModel):
    contract_version: Literal["skill-handoff-ingest-plan-v1"] = HANDOFF_INGEST_PLAN_VERSION
    plan_id: str
    handoff_id: str
    workspace_id: str
    project_id: str
    domain_profile: str
    profile_version: str
    source_id: str
    writes: list[MemoryWriteInput]
    deletes: list[MemoryDeleteInput]
    blocked_items: list[BlockedHandoffItem]
    pending_post_write_revocations: list[PendingPostWriteRevocation]
    already_applied: list[AlreadyAppliedItem]
    counts: HandoffIngestCounts
    source_units: list[HandoffSourceUnit] = Field(default_factory=list)
    source_registration_counts: HandoffSourceRegistrationCounts
    handoff_manifest_sha256: str
    approval_manifest_sha256: str
    ledger_sha256: str
    approved_batch_sha256: str
    data_artifact_hashes: dict[str, str]
    production_writes: bool = False
    canonical_writes: bool = False
    milvus_writes: bool = False
    alias_changes: bool = False
    real_llm_called: bool = False


class HandoffAppliedWrite(_StrictModel):
    idempotency_key: str
    memory_id: str
    status: str
    version: int
    created: bool
    deduplicated: bool
    evidence_ids: list[str] = Field(default_factory=list)
    outbox_ids: list[str] = Field(default_factory=list)


class HandoffAppliedDelete(_StrictModel):
    idempotency_key: str
    deleted_count: int
    memory_ids: list[str] = Field(default_factory=list)
    outbox_ids: list[str] = Field(default_factory=list)


class HandoffApplyItem(_StrictModel):
    item_type: Literal["write", "delete"]
    idempotency_key: str


class HandoffApplyFailure(_StrictModel):
    item: HandoffApplyItem
    error_type: str
    message: str = "item_failed"


class HandoffApplyProgress(_StrictModel):
    writes_succeeded: int
    deletes_succeeded: int
    write_outbox_ids: list[str] = Field(default_factory=list)
    delete_outbox_ids: list[str] = Field(default_factory=list)


class HandoffIngestApplyCounts(_StrictModel):
    planned_writes: int
    planned_deletes: int
    writes_succeeded: int
    deletes_succeeded: int
    already_applied: int
    blocked_items: int
    pending_post_write_revocations: int


class HandoffIngestApplyResult(_StrictModel):
    contract_version: Literal["skill-handoff-ingest-apply-v1"] = HANDOFF_INGEST_APPLY_VERSION
    status: Literal["completed", "stopped"]
    handoff_id: str
    plan_id: str
    write_results: list[HandoffAppliedWrite]
    delete_results: list[HandoffAppliedDelete]
    already_applied: list[AlreadyAppliedItem]
    blocked_items: list[BlockedHandoffItem]
    pending_post_write_revocations: list[PendingPostWriteRevocation]
    counts: HandoffIngestApplyCounts
    before_counts: dict[str, int]
    after_counts: dict[str, int]
    stopped_on_item: HandoffApplyItem | None = None
    failure: HandoffApplyFailure | None = None
    applied_before_stop: HandoffApplyProgress | None = None
    production_writes: bool = False
    canonical_writes: bool = False
    milvus_writes: bool = False
    alias_changes: bool = False
    real_llm_called: bool = False


class LocalHandoffAuthorizationReceipt(_StrictModel):
    contract_version: Literal["skill-handoff-ingest-local-apply-v1"] = HANDOFF_INGEST_LOCAL_APPLY_VERSION
    grant_id: str
    principal_id: str
    workspace_id: str
    project_id: str
    domain_profile: str
    profile_version: str
    actions: tuple[Literal["write", "delete"], ...]
    expires_at: datetime
    provider: Literal["local_ephemeral"] = "local_ephemeral"
    revoked_after_use: bool = True


class LocalHandoffApplyResult(_StrictModel):
    contract_version: Literal["skill-handoff-ingest-local-apply-v1"] = HANDOFF_INGEST_LOCAL_APPLY_VERSION
    status: Literal["completed", "stopped"]
    plan_id: str
    authorization: LocalHandoffAuthorizationReceipt
    apply_result: HandoffIngestApplyResult


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _approval_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _item_sha256(item: HandoffItem) -> str:
    return _sha256_bytes(_canonical_json(item.canonical_dict()).encode("utf-8"))

def _explicit_dir(path: str | Path) -> Path:
    raw = Path(path).expanduser()
    if raw.is_symlink():
        raise SkillApiBlocked("handoff directory must not be a symlink")
    try:
        resolved = raw.resolve(strict=True)
    except OSError as exc:
        raise SkillApiBlocked("handoff directory is missing") from exc
    if not resolved.is_dir():
        raise SkillApiBlocked("handoff path must be an existing directory")
    return resolved


def _explicit_file(root: Path, name: str) -> Path:
    path = root / name
    if path.is_symlink():
        raise SkillApiBlocked(f"{name} must not be a symlink")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise SkillApiBlocked(f"required handoff artifact is missing: {name}") from exc
    if not resolved.is_relative_to(root) or not resolved.is_file():
        raise SkillApiBlocked(f"handoff artifact escapes its directory: {name}")
    return resolved


def _read_bytes(root: Path, name: str) -> bytes:
    payload = _explicit_file(root, name).read_bytes()
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SkillApiBlocked(f"{name} must be UTF-8") from exc
    from .installer import scan_for_secrets
    scan = scan_for_secrets(text)
    if scan.status == "blocked":
        reasons = ",".join(scan.blocked_reasons)
        raise SkillApiBlocked(f"{name} blocked by secret scan: {reasons}")
    return payload


def _read_text(root: Path, name: str) -> str:
    return _read_bytes(root, name).decode("utf-8")


def _read_json(root: Path, name: str) -> dict[str, Any]:
    try:
        value = json.loads(_read_text(root, name))
    except ValueError as exc:
        raise SkillApiBlocked(f"{name} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise SkillApiBlocked(f"{name} must be a JSON object")
    return value


def _read_jsonl(root: Path, name: str) -> list[dict[str, Any]]:
    text = _read_text(root, name)
    values: list[dict[str, Any]] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            raise SkillApiBlocked(f"{name} line {line_number} is empty")
        try:
            value = json.loads(line)
        except ValueError as exc:
            raise SkillApiBlocked(f"{name} line {line_number} is not valid JSON") from exc
        if not isinstance(value, dict):
            raise SkillApiBlocked(f"{name} line {line_number} must be an object")
        values.append(value)
    return values


def _model_validate(model: type[BaseModel], value: Any, label: str) -> Any:
    try:
        return model.model_validate(value)
    except ValidationError as exc:
        raise SkillApiBlocked(f"{label} failed strict validation") from exc


def _parse_written_map(written_memory_map: Mapping[str, Any] | None) -> dict[str, WrittenMemoryRef]:
    if written_memory_map is None:
        raise SkillApiBlocked("written_memory_map must be explicitly provided")
    if not isinstance(written_memory_map, Mapping):
        raise SkillApiBlocked("written_memory_map must be an object")
    result: dict[str, WrittenMemoryRef] = {}
    memory_ids: set[str] = set()
    for key, value in written_memory_map.items():
        if not isinstance(key, str) or not key.strip():
            raise SkillApiBlocked("written_memory_map keys must be non-empty strings")
        ref = _model_validate(WrittenMemoryRef, value, "written_memory_map entry")
        if ref.memory_id in memory_ids:
            raise SkillApiBlocked("written_memory_map contains duplicate memory_id values")
        memory_ids.add(ref.memory_id)
        result[key] = ref
    return result


def _check_item_context(item: HandoffItem, manifest: HandoffManifest, *, workspace_id: str, project_id: str, domain_profile: str, profile_version: str, source_id: str) -> None:
    for name, actual, expected in (
        ("workspace_id", item.workspace_id, workspace_id),
        ("project_id", item.project_id, project_id),
        ("domain_profile", item.domain_profile, domain_profile),
        ("profile_version", item.profile_version, profile_version),
        ("source_id", item.source_id, source_id),
    ):
        if actual != expected:
            raise SkillApiBlocked(f"handoff item {name} does not match the requested scope")
    if item.conversation_id and item.conversation_id not in manifest.conversation_ids:
        raise SkillApiBlocked("handoff item conversation_id is not declared by the handoff manifest")


def _evidence_payload(item: HandoffItem) -> list[dict[str, Any]]:
    result = []
    for evidence in item.evidences:
        locator = dict(evidence.locator_json)
        existing = locator.get("handoff_evidence_id")
        if existing is not None and existing != evidence.evidence_id:
            raise SkillApiBlocked("evidence locator conflicts with handoff_evidence_id")
        locator["handoff_evidence_id"] = evidence.evidence_id
        material = {"contract_version": HANDOFF_CONTRACT_VERSION, "idempotency_key": item.idempotency_key, "handoff_evidence_id": evidence.evidence_id}
        evidence_id = "ev_" + _sha256_bytes(_canonical_json(material).encode("utf-8"))[:32]
        result.append({
            "evidence_id": evidence_id,
            "source_unit_id": evidence.source_unit_id,
            "quote": evidence.quote,
            "locator_json": locator,
            "status": evidence.status,
        })
    return result


def _to_write(item: HandoffItem, *, handoff_id: str, approval: HandoffApprovalRecord, written: dict[str, WrittenMemoryRef]) -> MemoryWriteInput:
    domain_payload = dict(item.domain_payload)
    domain_payload["conversation_handoff"] = {
        "contract_version": HANDOFF_CONTRACT_VERSION,
        "handoff_id": handoff_id,
        "claim_key": item.claim_key,
        "scope_key": item.scope_key,
        "observed_at": item.observed_at,
        "transition_kind": item.transition_kind,
        "supersedes_ref": item.supersedes_ref,
        "approval_id": approval.approval_id,
    }
    supersedes_id = item.supersedes_id
    expected_version = item.expected_version
    if item.supersedes_ref:
        ref = written.get(item.supersedes_ref)
        if ref is None:
            raise SkillApiBlocked(f"supersedes_ref has no canonical or explicit written_memory_map mapping: {item.supersedes_ref}")
        if supersedes_id is not None and supersedes_id != ref.memory_id:
            raise SkillApiBlocked("handoff supersedes_id conflicts with written_memory_map")
        if expected_version is not None and expected_version != ref.version:
            raise SkillApiBlocked("expected_version conflicts with written_memory_map")
        supersedes_id = ref.memory_id
        expected_version = ref.version
    payload = {
        "idempotency_key": item.idempotency_key,
        "operation_id": item.operation_id,
        "workspace_id": item.workspace_id,
        "project_id": item.project_id,
        "task_id": item.task_id,
        "conversation_id": item.conversation_id,
        "domain_profile": item.domain_profile,
        "profile_version": item.profile_version,
        "memory_type": item.memory_type,
        "subtype": item.subtype,
        "subject": item.subject,
        "entity_type": item.entity_type,
        "entity_id": item.entity_id,
        "content": item.content,
        "importance": item.importance,
        "declared_sensitivity": item.declared_sensitivity,
        "requested_visibility": item.requested_visibility,
        "assertion_source": item.assertion_source,
        "verification_state": item.verification_state,
        "effective_from": item.effective_from,
        "effective_to": item.effective_to,
        "source_id": item.source_id,
        "source_unit_id": item.source_unit_id,
        "source_scope": item.source_scope.model_dump(mode="json"),
        "confidence": item.confidence,
        "supersedes_id": supersedes_id,
        "expected_version": expected_version,
        "domain_payload": domain_payload,
        "evidences": _evidence_payload(item),
    }
    try:
        return MemoryWriteInput.model_validate(payload)
    except ValidationError as exc:
        raise SkillApiBlocked(f"approved handoff item is not a valid MemoryWriteInput: {item.idempotency_key}") from exc


def _delete_input(approval: HandoffApprovalRecord, *, workspace_id: str, ref: WrittenMemoryRef) -> MemoryDeleteInput:
    material = {"approval_id": approval.approval_id, "workspace_id": workspace_id, "target_type": "memory_id", "target_id": ref.memory_id, "expected_version": ref.version}
    digest = _sha256_bytes(_canonical_json(material).encode("utf-8"))
    return MemoryDeleteInput(
        idempotency_key="mdel_" + digest[:32],
        operation_id="op_del_" + digest[:24],
        workspace_id=workspace_id,
        target_type="memory_id",
        target_id=ref.memory_id,
        delete_reason=approval.reason,
        expected_version=ref.version,
    )


def _sort_item(item: HandoffItem) -> tuple[str, str, int, str]:
    sequence = item.domain_payload.get("source_sequence", -1)
    if isinstance(sequence, bool) or not isinstance(sequence, int):
        sequence = -1
    return (item.conversation_id or "", item.observed_at, sequence, item.idempotency_key)

def _source_descriptor(unit: HandoffSourceUnit) -> tuple[str, str, str, str, str, str, str, str]:
    return (
        unit.workspace_id,
        unit.project_id,
        unit.source_id,
        unit.source_type,
        unit.source_path,
        unit.source_sha256,
        unit.visibility,
        unit.sensitivity.value,
    )


def _validate_source_unit_for_item(
    item: HandoffItem,
    unit: HandoffSourceUnit,
    *,
    handoff_manifest: HandoffManifest,
) -> None:
    if unit.workspace_id != item.workspace_id:
        raise SkillApiBlocked("source unit workspace does not match approved item")
    if unit.project_id != item.project_id:
        raise SkillApiBlocked("source unit project does not match approved item")
    if unit.source_id != item.source_id or unit.source_id != handoff_manifest.source_id:
        raise SkillApiBlocked("source unit source_id does not match approved item/manifest")
    if unit.source_sha256 != handoff_manifest.source_sha256:
        raise SkillApiBlocked("source unit source_sha256 does not match handoff manifest")
    exact_scope = unit.scope_type == item.source_scope.scope_type and unit.scope_ref == item.source_scope.ref
    observation_under_message = (
        unit.scope_type == "conversation_message"
        and item.source_scope.scope_type == "conversation_observation"
    )
    if not exact_scope and not observation_under_message:
        raise SkillApiBlocked("source unit scope does not match approved item")
    if unit.conversation_id != item.conversation_id:
        raise SkillApiBlocked("source unit conversation_id does not match approved item")
    expected_visibility = "private" if item.requested_visibility == "private" else "workspace"
    if item.requested_visibility not in {"private", "workspace", "shared"}:
        raise SkillApiBlocked("approved item has unsupported requested_visibility")
    if unit.visibility != expected_visibility:
        raise SkillApiBlocked("source unit visibility does not match approved item")
    try:
        item_sensitivity = SensitivityLevel(item.declared_sensitivity)
    except ValueError as exc:
        raise SkillApiBlocked("approved item has unsupported declared_sensitivity") from exc
    if unit.sensitivity != item_sensitivity:
        raise SkillApiBlocked("source unit sensitivity does not match approved item")


def plan_handoff_ingest(
    *,
    handoff_dir: str | Path,
    handoff_id: str,
    workspace_id: str,
    project_id: str,
    domain_profile: str,
    profile_version: str,
    source_id: str,
    written_memory_map: Mapping[str, Any] | None,
) -> HandoffIngestPlan:
    """Read and independently validate a handoff approval directory; never writes."""

    directory = _explicit_dir(handoff_dir)
    handoff_manifest_bytes = _read_bytes(directory, HANDOFF_MANIFEST)
    try:
        handoff_manifest_raw = json.loads(handoff_manifest_bytes.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise SkillApiBlocked("handoff_manifest.json is not valid UTF-8 JSON") from exc
    if not isinstance(handoff_manifest_raw, dict):
        raise SkillApiBlocked("handoff_manifest.json must be a JSON object")
    handoff_manifest = _model_validate(HandoffManifest, handoff_manifest_raw, "handoff_manifest.json")
    if handoff_id != handoff_manifest.handoff_id:
        raise SkillApiBlocked("requested handoff_id does not match handoff_manifest.json")
    for name, actual, expected in (
        ("workspace_id", handoff_manifest.workspace_id, workspace_id),
        ("project_id", handoff_manifest.project_id, project_id),
        ("domain_profile", handoff_manifest.domain_profile, domain_profile),
        ("profile_version", handoff_manifest.profile_version, profile_version),
        ("source_id", handoff_manifest.source_id, source_id),
    ):
        if actual != expected:
            raise SkillApiBlocked(f"handoff manifest {name} does not match the requested scope")

    artifact_hashes: dict[str, str] = {}
    for name in HANDOFF_DATA_ARTIFACTS:
        artifact_hashes[name] = _sha256_bytes(_read_bytes(directory, name))
    if artifact_hashes != handoff_manifest.artifacts:
        raise SkillApiBlocked("handoff data artifact hash does not match handoff_manifest.json")

    item_values = _read_jsonl(directory, "memory_write_batch.jsonl")
    items: dict[str, HandoffItem] = {}
    for line_number, value in enumerate(item_values, start=1):
        item = _model_validate(HandoffItem, value, f"memory_write_batch.jsonl line {line_number}")
        if item.idempotency_key in items:
            raise SkillApiBlocked("memory_write_batch.jsonl contains duplicate idempotency keys")
        _check_item_context(item, handoff_manifest, workspace_id=workspace_id, project_id=project_id, domain_profile=domain_profile, profile_version=profile_version, source_id=source_id)
        items[item.idempotency_key] = item

    source_unit_values = _read_jsonl(directory, "source_units.jsonl")
    source_units_by_id: dict[str, HandoffSourceUnit] = {}
    source_descriptors: dict[str, tuple[str, str, str, str, str, str, str, str]] = {}
    for line_number, value in enumerate(source_unit_values, start=1):
        unit = _model_validate(HandoffSourceUnit, value, f"source_units.jsonl line {line_number}")
        if unit.source_unit_id in source_units_by_id:
            raise SkillApiBlocked("source_units.jsonl contains duplicate source_unit_id")
        descriptor = _source_descriptor(unit)
        existing_descriptor = source_descriptors.get(unit.source_id)
        if existing_descriptor is not None and existing_descriptor != descriptor:
            raise SkillApiBlocked("source_units.jsonl contains conflicting source descriptors")
        source_descriptors[unit.source_id] = descriptor
        source_units_by_id[unit.source_unit_id] = unit

    written = _parse_written_map(written_memory_map)
    for key in written:
        if key not in items:
            raise SkillApiBlocked("written_memory_map references an unknown handoff item")

    approval_manifest_bytes = _read_bytes(directory, APPROVAL_MANIFEST)
    try:
        approval_manifest_raw = json.loads(approval_manifest_bytes.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise SkillApiBlocked("approval_manifest.json is not valid UTF-8 JSON") from exc
    if not isinstance(approval_manifest_raw, dict):
        raise SkillApiBlocked("approval_manifest.json must be a JSON object")
    approval_manifest = _model_validate(HandoffApprovalManifest, approval_manifest_raw, "approval_manifest.json")
    if approval_manifest.handoff_id != handoff_id:
        raise SkillApiBlocked("approval_manifest handoff_id does not match")
    approval_manifest_sha = _sha256_bytes(approval_manifest_bytes)
    if handoff_manifest.approval_manifest_sha256 is not None and handoff_manifest.approval_manifest_sha256 != approval_manifest_sha:
        raise SkillApiBlocked("handoff manifest approval_manifest_sha256 does not match")
    ledger_raw = _read_bytes(directory, APPROVAL_LEDGER)
    batch_raw = _read_bytes(directory, APPROVED_BATCH)
    ledger_sha = _sha256_bytes(ledger_raw)
    batch_sha = _sha256_bytes(batch_raw)
    if approval_manifest.ledger_sha256 != ledger_sha or approval_manifest.approved_batch_sha256 != batch_sha:
        raise SkillApiBlocked("approval manifest artifact hash does not match approval artifacts")

    approvals: list[HandoffApprovalRecord] = []
    seen_approval_ids: set[str] = set()
    for line_number, value in enumerate(_read_jsonl(directory, APPROVAL_LEDGER), start=1):
        approval = _model_validate(HandoffApprovalRecord, value, f"approval_ledger.jsonl line {line_number}")
        if approval.approval_id in seen_approval_ids:
            raise SkillApiBlocked("approval_ledger.jsonl contains duplicate approval_id")
        seen_approval_ids.add(approval.approval_id)
        if approval.handoff_id != handoff_id:
            raise SkillApiBlocked("approval ledger record belongs to a different handoff")
        item = items.get(approval.item_idempotency_key)
        if item is None:
            raise SkillApiBlocked("approval ledger record references an unknown handoff item")
        if approval.item_sha256 != _item_sha256(item):
            raise SkillApiBlocked("approval ledger item_sha256 does not match the current handoff item")
        approvals.append(approval)

    latest: dict[str, HandoffApprovalRecord] = {}
    for approval in approvals:
        current = latest.get(approval.item_idempotency_key)
        if current is None or (_approval_time(approval.reviewed_at), approval.approval_id) > (_approval_time(current.reviewed_at), current.approval_id):
            latest[approval.item_idempotency_key] = approval
    same_time: dict[tuple[str, str, datetime], str] = {}
    for approval in approvals:
        key = (approval.handoff_id, approval.item_idempotency_key, _approval_time(approval.reviewed_at))
        if key in same_time and same_time[key] != approval.decision:
            raise SkillApiBlocked("approval ledger has conflicting decisions at the same reviewed_at")
        same_time[key] = approval.decision

    approved_batch_values = _read_jsonl(directory, APPROVED_BATCH) if batch_raw else []
    approved_batch: dict[str, HandoffItem] = {}
    for line_number, value in enumerate(approved_batch_values, start=1):
        item = _model_validate(HandoffItem, value, f"approved_memory_write_batch.jsonl line {line_number}")
        if item.idempotency_key in approved_batch:
            raise SkillApiBlocked("approved batch contains duplicate idempotency keys")
        current = items.get(item.idempotency_key)
        if current is None or item.canonical_dict() != current.canonical_dict():
            raise SkillApiBlocked("approved batch item does not match current handoff item")
        approved_batch[item.idempotency_key] = item
    expected_approved = {key for key, approval in latest.items() if approval.decision == "approve"}
    if set(approved_batch) != expected_approved:
        raise SkillApiBlocked("approved batch does not match the latest ledger approvals")

    if approval_manifest.item_count != len(items):
        raise SkillApiBlocked("approval_manifest item_count does not match the handoff item count")
    decision_counts = {"approved": 0, "rejected": 0, "deferred": 0, "revoked": 0, "pending": 0}
    for item in items.values():
        approval = latest.get(item.idempotency_key)
        if approval is None:
            decision_counts["pending"] += 1
        else:
            decision_counts[{"approve": "approved", "reject": "rejected", "defer": "deferred", "revoke": "revoked"}[approval.decision]] += 1
    for count_name in ("approved", "rejected", "deferred", "revoked", "pending"):
        if getattr(approval_manifest, f"{count_name}_count") != decision_counts[count_name]:
            raise SkillApiBlocked(f"approval_manifest {count_name}_count does not match the ledger")

    approved_item_keys = {key for key, approval in latest.items() if approval.decision == "approve"}
    approved_source_units: dict[str, HandoffSourceUnit] = {}
    for item in sorted(items.values(), key=_sort_item):
        if item.idempotency_key not in approved_item_keys:
            continue
        primary_unit = source_units_by_id.get(item.source_unit_id)
        if primary_unit is None:
            raise SkillApiBlocked("approved item references a missing source unit")
        _validate_source_unit_for_item(item, primary_unit, handoff_manifest=handoff_manifest)
        approved_source_units[primary_unit.source_unit_id] = primary_unit
        if not item.evidences:
            raise SkillApiBlocked("approved item must include verified evidence")
        for evidence in item.evidences:
            unit = source_units_by_id.get(evidence.source_unit_id)
            if unit is None:
                raise SkillApiBlocked("approved item evidence references a missing source unit")
            if evidence.status != "verified":
                raise SkillApiBlocked("approved item contains non-verified evidence")
            _validate_source_unit_for_item(item, unit, handoff_manifest=handoff_manifest)
            if (
                unit.scope_type == "conversation_message"
                and item.source_scope.scope_type == "conversation_observation"
            ):
                evidence_message_id = evidence.locator_json.get("message_id")
                if not isinstance(evidence_message_id, str) or evidence_message_id != unit.message_id:
                    raise SkillApiBlocked("observation evidence message_id does not match source unit message")
            if evidence.quote not in unit.text_content:
                raise SkillApiBlocked("approved evidence quote is not an exact substring of source unit text")
            approved_source_units[unit.source_unit_id] = unit

    registration_source_units = sorted(
        approved_source_units.values(),
        key=lambda unit: (unit.source_id, unit.sequence, unit.source_unit_id),
    )
    source_registration_counts = HandoffSourceRegistrationCounts(
        sources=len({unit.source_id for unit in registration_source_units}),
        source_units=len(registration_source_units),
    )

    prior_approve = {approval.item_idempotency_key for approval in approvals if approval.decision == "approve"}
    writes: list[MemoryWriteInput] = []
    deletes: list[MemoryDeleteInput] = []
    blocked: list[BlockedHandoffItem] = []
    pending: list[PendingPostWriteRevocation] = []
    already: list[AlreadyAppliedItem] = []
    for item in sorted(items.values(), key=_sort_item):
        approval = latest.get(item.idempotency_key)
        if approval is None:
            blocked.append(BlockedHandoffItem(idempotency_key=item.idempotency_key, decision="pending", reason="no_decision", item_sha256=_item_sha256(item)))
        elif approval.decision == "approve":
            ref = written.get(item.idempotency_key)
            if ref is not None:
                candidate = _to_write(item, handoff_id=handoff_id, approval=approval, written=written)
                if ref.request_sha256 is not None:
                    candidate_sha256 = _sha256_bytes(
                        _canonical_json(candidate.model_dump(mode="json")).encode("utf-8")
                    )
                    if not hmac.compare_digest(candidate_sha256, ref.request_sha256):
                        raise SkillApiBlocked(
                            f"idempotency key was reused with a different request: {item.idempotency_key}"
                        )
                already.append(AlreadyAppliedItem(idempotency_key=item.idempotency_key, approval_id=approval.approval_id, memory_id=ref.memory_id, version=ref.version))
            else:
                writes.append(_to_write(item, handoff_id=handoff_id, approval=approval, written=written))
        elif approval.decision == "revoke":
            ref = written.get(item.idempotency_key)
            if ref is not None:
                deletes.append(_delete_input(approval, workspace_id=workspace_id, ref=ref))
            elif item.idempotency_key in prior_approve:
                pending.append(PendingPostWriteRevocation(idempotency_key=item.idempotency_key, approval_id=approval.approval_id, item_sha256=_item_sha256(item), reason=approval.reason))
            else:
                blocked.append(BlockedHandoffItem(idempotency_key=item.idempotency_key, decision="revoke", reason="revoked_before_write", approval_id=approval.approval_id, item_sha256=_item_sha256(item)))
        else:
            blocked.append(BlockedHandoffItem(idempotency_key=item.idempotency_key, decision=approval.decision, reason=f"latest_decision_{approval.decision}", approval_id=approval.approval_id, item_sha256=_item_sha256(item)))

    blocked.sort(key=lambda row: row.idempotency_key)
    already.sort(key=lambda row: row.idempotency_key)
    pending.sort(key=lambda row: row.idempotency_key)
    counts = HandoffIngestCounts(items=len(items), writes=len(writes), deletes=len(deletes), already_applied=len(already), blocked_items=len(blocked), pending_post_write_revocations=len(pending))
    material = {
        "handoff_id": handoff_id,
        "workspace_id": workspace_id,
        "project_id": project_id,
        "domain_profile": domain_profile,
        "profile_version": profile_version,
        "source_id": source_id,
        "writes": [w.model_dump(mode="json") for w in writes],
        "deletes": [d.model_dump(mode="json") for d in deletes],
        "blocked": [b.model_dump(mode="json") for b in blocked],
        "pending": [p.model_dump(mode="json") for p in pending],
        "already": [a.model_dump(mode="json") for a in already],
        "source_units": [u.model_dump(mode="json") for u in registration_source_units],
        "hashes": [handoff_manifest.artifacts, ledger_sha, batch_sha, approval_manifest_sha],
    }
    plan_id = "handoff_plan_" + _sha256_bytes(_canonical_json(material).encode("utf-8"))[:32]
    return HandoffIngestPlan(
        plan_id=plan_id,
        handoff_id=handoff_id,
        workspace_id=workspace_id,
        project_id=project_id,
        domain_profile=domain_profile,
        profile_version=profile_version,
        source_id=source_id,
        writes=writes,
        deletes=deletes,
        blocked_items=blocked,
        pending_post_write_revocations=pending,
        already_applied=already,
        counts=counts,
        source_units=registration_source_units,
        source_registration_counts=source_registration_counts,
        handoff_manifest_sha256=_sha256_bytes(handoff_manifest_bytes),
        approval_manifest_sha256=approval_manifest_sha,
        ledger_sha256=ledger_sha,
        approved_batch_sha256=batch_sha,
        data_artifact_hashes=artifact_hashes,
    )


def _derive_item_token(token: AgentContextToken, validator: TokenValidator) -> AgentContextToken:
    unsigned = token.model_copy(update={"jti": uuid.uuid4().hex, "signature": "placeholder"})
    return unsigned.model_copy(update={"signature": sign_token(unsigned, validator.secret)})


def _apply_counts(plan: HandoffIngestPlan, *, writes_succeeded: int, deletes_succeeded: int) -> HandoffIngestApplyCounts:
    return HandoffIngestApplyCounts(
        planned_writes=len(plan.writes),
        planned_deletes=len(plan.deletes),
        writes_succeeded=writes_succeeded,
        deletes_succeeded=deletes_succeeded,
        already_applied=len(plan.already_applied),
        blocked_items=len(plan.blocked_items),
        pending_post_write_revocations=len(plan.pending_post_write_revocations),
    )


def _register_handoff_sources(
    *,
    plan: HandoffIngestPlan,
    store: SQLiteStore,
    token_validator: TokenValidator,
    token: AgentContextToken,
    domain_profile: str,
) -> None:
    if not plan.source_units:
        return
    registration_token = _derive_item_token(token, token_validator)
    context = token_validator.validate(registration_token, action="write", consume_query=False)
    grant = context.grant

    for unit in plan.source_units:
        if unit.workspace_id != token.workspace_id or unit.workspace_id != grant.workspace_id:
            raise SkillApiBlocked("source unit workspace is outside the write grant")
        if grant.scope.project_ids and unit.project_id not in grant.scope.project_ids:
            raise SkillApiBlocked("source unit project is outside the write grant")
        if grant.scope.domains and domain_profile not in grant.scope.domains:
            raise SkillApiBlocked("source unit domain is outside the write grant")

    with store.transaction() as connection:
        for unit in plan.source_units:
            source_row = connection.execute(
                "SELECT * FROM sources WHERE source_id=?", (unit.source_id,)
            ).fetchone()
            expected_owner = token.principal_id if unit.visibility == "private" else None
            if source_row is None:
                store.insert_source(
                    source_id=unit.source_id,
                    workspace_id=unit.workspace_id,
                    project_id=unit.project_id,
                    visibility=unit.visibility,
                    owner_principal_id=expected_owner,
                    source_path=unit.source_path,
                    source_sha256=unit.source_sha256,
                    source_type=unit.source_type,
                    sensitivity=unit.sensitivity.value,
                    connection=connection,
                )
            else:
                source_matches = (
                    source_row["workspace_id"] == unit.workspace_id
                    and source_row["project_id"] == unit.project_id
                    and source_row["source_path"] == unit.source_path
                    and source_row["source_sha256"] == unit.source_sha256
                    and source_row["source_type"] == unit.source_type
                    and source_row["visibility"] == unit.visibility
                    and source_row["sensitivity"] == unit.sensitivity.value
                )
                if unit.visibility == "private":
                    source_matches = source_matches and source_row["owner_principal_id"] == token.principal_id
                if not source_matches:
                    raise SkillApiBlocked("existing source does not match source_units.jsonl")

            unit_row = connection.execute(
                "SELECT * FROM source_units WHERE source_unit_id=?", (unit.source_unit_id,)
            ).fetchone()
            expected_metadata = unit.registration_metadata()
            if unit_row is None:
                store.insert_source_unit(
                    source_unit_id=unit.source_unit_id,
                    source_id=unit.source_id,
                    project_id=unit.project_id,
                    parent_unit_id=unit.parent_unit_id,
                    scope_type=unit.scope_type,
                    scope_ref=unit.scope_ref,
                    source_unit_hash=unit.source_unit_hash,
                    text_content=unit.text_content,
                    metadata=expected_metadata,
                    connection=connection,
                )
            else:
                unit_matches = (
                    unit_row["source_id"] == unit.source_id
                    and unit_row["parent_unit_id"] == unit.parent_unit_id
                    and unit_row["project_id"] == unit.project_id
                    and unit_row["scope_type"] == unit.scope_type
                    and unit_row["scope_ref"] == unit.scope_ref
                    and unit_row["source_unit_hash"] == unit.source_unit_hash
                    and unit_row["text_content"] == unit.text_content
                    and json_loads(unit_row["metadata_json"], {}) == expected_metadata
                )
                if not unit_matches:
                    raise SkillApiBlocked("existing source unit does not match source_units.jsonl")


def apply_handoff_ingest(
    *,
    handoff_dir: str | Path,
    handoff_id: str,
    workspace_id: str,
    project_id: str,
    domain_profile: str,
    profile_version: str,
    source_id: str,
    written_memory_map: Mapping[str, Any] | None,
    store: SQLiteStore,
    profiles: ProfileRegistry,
    token_validator: TokenValidator,
    token: AgentContextToken,
    embedding_model: str = "fake-bge-m3-v1",
    embedding_dim: int = 1024,
    index_schema_version: str = "memory-index-v1",
) -> HandoffIngestApplyResult:
    """Revalidate a handoff plan and apply its items with per-item transactions."""

    if not hmac.compare_digest(sign_token(token, token_validator.secret), token.signature):
        raise PermissionError("invalid token signature")
    plan = plan_handoff_ingest(
        handoff_dir=handoff_dir,
        handoff_id=handoff_id,
        workspace_id=workspace_id,
        project_id=project_id,
        domain_profile=domain_profile,
        profile_version=profile_version,
        source_id=source_id,
        written_memory_map=written_memory_map,
    )
    _register_handoff_sources(
        plan=plan,
        store=store,
        token_validator=token_validator,
        token=token,
        domain_profile=domain_profile,
    )
    before_counts = store.reconciliation_counts(workspace_id=workspace_id)
    writer = MemoryWriter(
        store=store,
        profiles=profiles,
        token_validator=token_validator,
        embedding_model=embedding_model,
        embedding_dim=embedding_dim,
        index_schema_version=index_schema_version,
    )
    delete_service = MemoryDeleteService(store=store, token_validator=token_validator)
    write_results: list[HandoffAppliedWrite] = []
    delete_results: list[HandoffAppliedDelete] = []
    write_outbox_ids: list[str] = []
    delete_outbox_ids: list[str] = []
    stopped_on_item: HandoffApplyItem | None = None
    failure: HandoffApplyFailure | None = None

    for item in plan.writes:
        apply_item = HandoffApplyItem(item_type="write", idempotency_key=item.idempotency_key)
        try:
            result = writer.write(item, _derive_item_token(token, token_validator))
        except Exception as exc:
            stopped_on_item = apply_item
            failure = HandoffApplyFailure(item=apply_item, error_type=type(exc).__name__)
            break
        write_results.append(HandoffAppliedWrite(
            idempotency_key=item.idempotency_key,
            memory_id=result.memory_id,
            status=result.status.value,
            version=result.version,
            created=result.created,
            deduplicated=result.deduplicated,
            evidence_ids=list(result.evidence_ids),
            outbox_ids=list(result.outbox_ids),
        ))
        write_outbox_ids.extend(result.outbox_ids)

    if failure is None:
        for item in plan.deletes:
            apply_item = HandoffApplyItem(item_type="delete", idempotency_key=item.idempotency_key)
            try:
                result = delete_service.delete(item, _derive_item_token(token, token_validator))
            except Exception as exc:
                stopped_on_item = apply_item
                failure = HandoffApplyFailure(item=apply_item, error_type=type(exc).__name__)
                break
            delete_results.append(HandoffAppliedDelete(
                idempotency_key=item.idempotency_key,
                deleted_count=result.deleted_count,
                memory_ids=list(result.memory_ids),
                outbox_ids=list(result.outbox_ids),
            ))
            delete_outbox_ids.extend(result.outbox_ids)

    after_counts = store.reconciliation_counts(workspace_id=workspace_id)
    counts = _apply_counts(plan, writes_succeeded=len(write_results), deletes_succeeded=len(delete_results))
    progress = HandoffApplyProgress(
        writes_succeeded=len(write_results),
        deletes_succeeded=len(delete_results),
        write_outbox_ids=write_outbox_ids,
        delete_outbox_ids=delete_outbox_ids,
    ) if write_results or delete_results else None
    return HandoffIngestApplyResult(
        status="stopped" if failure is not None else "completed",
        handoff_id=handoff_id,
        plan_id=plan.plan_id,
        write_results=write_results,
        delete_results=delete_results,
        already_applied=list(plan.already_applied),
        blocked_items=list(plan.blocked_items),
        pending_post_write_revocations=list(plan.pending_post_write_revocations),
        counts=counts,
        before_counts=before_counts,
        after_counts=after_counts,
        stopped_on_item=stopped_on_item,
        failure=failure,
        applied_before_stop=progress if failure is not None else None,
    )

def _validated_local_apply_store(
    canonical_db: str | Path,
    *,
    workspace_id: str,
    project_id: str,
    domain_profile: str,
    profile_version: str,
    profiles: ProfileRegistry,
) -> SQLiteStore:
    raw = Path(canonical_db).expanduser()
    if raw.is_symlink():
        raise SkillApiBlocked("canonical_db must not be a symlink")
    try:
        resolved = raw.resolve(strict=True)
    except OSError as exc:
        raise SkillApiBlocked("canonical_db does not exist") from exc
    if not resolved.is_file():
        raise SkillApiBlocked("canonical_db must be an existing SQLite file")

    try:
        profile = profiles.get(domain_profile, profile_version)
    except KeyError as exc:
        raise SkillApiBlocked("domain_profile/profile_version is not registered") from exc

    migration_paths = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if not migration_paths:
        raise SkillApiBlocked("no schema migrations are available")
    expected_migrations = {
        path.stem: hashlib.sha256(path.read_text(encoding="utf-8").encode("utf-8")).hexdigest()
        for path in migration_paths
    }

    store = SQLiteStore(resolved)
    try:
        connection = store.connect(read_only=True)
    except sqlite3.DatabaseError as exc:
        raise SkillApiBlocked("canonical_db is not a readable SQLite database") from exc
    try:
        quick_check = connection.execute("PRAGMA quick_check").fetchone()
        if quick_check is None or quick_check[0] != "ok":
            raise SkillApiBlocked("canonical_db integrity check failed")
        tables = {
            row["name"]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "schema_migrations" not in tables:
            raise SkillApiBlocked("canonical_db is not migrated")
        applied = {
            row["migration_id"]: row["checksum"]
            for row in connection.execute("SELECT migration_id,checksum FROM schema_migrations")
        }
        if applied != expected_migrations:
            raise SkillApiBlocked("canonical_db migration state does not match the current schema")
        workspace = connection.execute(
            "SELECT 1 FROM workspaces WHERE workspace_id=?", (workspace_id,)
        ).fetchone()
        if workspace is None:
            raise SkillApiBlocked("workspace_id is not present in canonical_db")
        project = connection.execute(
            "SELECT 1 FROM projects WHERE project_id=? AND workspace_id=?",
            (project_id, workspace_id),
        ).fetchone()
        if project is None:
            raise SkillApiBlocked("project_id is not present in the requested workspace")
        registered_profile = connection.execute(
            "SELECT domain FROM domain_profiles WHERE profile_id=? AND version=?",
            (domain_profile, profile_version),
        ).fetchone()
        if registered_profile is None or registered_profile["domain"] != profile.domain:
            raise SkillApiBlocked("domain profile is not registered in canonical_db")
    except sqlite3.DatabaseError as exc:
        raise SkillApiBlocked("canonical_db schema validation failed") from exc
    finally:
        connection.close()
    return store


def _preflight_local_delete_scope(
    store: SQLiteStore,
    deletes: list[MemoryDeleteInput],
    *,
    workspace_id: str,
    project_id: str,
    domain_profile: str,
    principal_id: str,
) -> None:
    if not deletes:
        return
    connection = store.connect(read_only=True)
    try:
        for item in deletes:
            if item.target_type != "memory_id":
                raise SkillApiBlocked("local handoff apply only supports memory_id deletes")
            row = connection.execute(
                """SELECT workspace_id,project_id,domain_profile,visibility,owner_principal_id
                   FROM memory_items WHERE memory_id=?""",
                (item.target_id,),
            ).fetchone()
            if row is None:
                continue
            if row["workspace_id"] != workspace_id or row["project_id"] != project_id:
                raise SkillApiBlocked("delete target is outside the requested workspace/project scope")
            if row["domain_profile"] != domain_profile:
                raise SkillApiBlocked("delete target is outside the requested domain scope")
            if row["visibility"] == "private" and row["owner_principal_id"] != principal_id:
                raise SkillApiBlocked("delete target belongs to another private owner")
    finally:
        connection.close()


def resolve_written_memory_map_from_canonical(
    *,
    canonical_db: str | Path,
    handoff_dir: str | Path,
    workspace_id: str,
    project_id: str,
    domain_profile: str,
    profile_version: str,
    principal_id: str,
    explicit_written_memory_map: Mapping[str, Any] | None = None,
    explicit_source: str | None = None,
) -> tuple[dict[str, dict[str, Any]], str]:
    """Merge canonical write idempotency records with an optional explicit override.

    The canonical SQLite database is the durable source of the
    ``idempotency_key -> memory_id/version`` mapping. Explicit entries are
    accepted only when they agree with a canonical record or have no canonical
    record yet. Every canonical record returned here is scoped to the requested
    workspace, project, domain, profile version, and owner.
    """

    if not isinstance(principal_id, str) or not principal_id.strip():
        raise SkillApiBlocked("principal_id must be non-empty")
    directory = _explicit_dir(handoff_dir)
    item_values = _read_jsonl(directory, "memory_write_batch.jsonl")
    item_keys: list[str] = []
    seen_keys: set[str] = set()
    for line_number, value in enumerate(item_values, start=1):
        key = value.get("idempotency_key")
        if not isinstance(key, str) or not key.strip():
            raise SkillApiBlocked(
                f"memory_write_batch.jsonl line {line_number} idempotency_key must be a non-empty string"
            )
        if key in seen_keys:
            raise SkillApiBlocked("memory_write_batch.jsonl contains duplicate idempotency keys")
        seen_keys.add(key)
        item_keys.append(key)

    explicit = (
        _parse_written_map(explicit_written_memory_map)
        if explicit_written_memory_map is not None
        else {}
    )
    unknown_explicit = set(explicit).difference(seen_keys)
    if unknown_explicit:
        raise SkillApiBlocked("written_memory_map references an unknown handoff item")

    automatic: dict[str, WrittenMemoryRef] = {}
    if item_keys:
        profiles = ProfileRegistry()
        store = _validated_local_apply_store(
            canonical_db,
            workspace_id=workspace_id,
            project_id=project_id,
            domain_profile=domain_profile,
            profile_version=profile_version,
            profiles=profiles,
        )
        with store.read() as connection:
            for offset in range(0, len(item_keys), 400):
                chunk = item_keys[offset:offset + 400]
                placeholders = ",".join("?" for _ in chunk)
                rows = connection.execute(
                    f"""SELECT idempotency_key,status,request_sha256,result_json
                        FROM idempotency_records
                        WHERE workspace_id=? AND principal_id=?
                          AND operation_type='write'
                          AND idempotency_key IN ({placeholders})
                        ORDER BY idempotency_key""",
                    (workspace_id, principal_id, *chunk),
                ).fetchall()
                for row in rows:
                    if row["status"] not in {"completed", "deduplicated"}:
                        raise SkillApiBlocked(
                            f"canonical write idempotency record is not completed: {row['idempotency_key']}"
                        )
                    try:
                        result = json.loads(row["result_json"])
                    except (TypeError, ValueError) as exc:
                        raise SkillApiBlocked(
                            f"canonical write idempotency result is invalid: {row['idempotency_key']}"
                        ) from exc
                    if not isinstance(result, dict):
                        raise SkillApiBlocked(
                            f"canonical write idempotency result must be an object: {row['idempotency_key']}"
                        )
                    memory_id = result.get("memory_id")
                    version = result.get("version")
                    if not isinstance(memory_id, str) or not memory_id.strip():
                        raise SkillApiBlocked(
                            f"canonical write idempotency result has no memory_id: {row['idempotency_key']}"
                        )
                    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
                        raise SkillApiBlocked(
                            f"canonical write idempotency result has an invalid version: {row['idempotency_key']}"
                        )
                    memory = connection.execute(
                        """SELECT memory_id,workspace_id,project_id,owner_principal_id,
                                  domain_profile,profile_version
                           FROM memory_items WHERE memory_id=?""",
                        (memory_id,),
                    ).fetchone()
                    if memory is None:
                        raise SkillApiBlocked(
                            f"canonical write idempotency result points to a missing memory: {row['idempotency_key']}"
                        )
                    if (
                        memory["workspace_id"] != workspace_id
                        or memory["project_id"] != project_id
                        or memory["owner_principal_id"] != principal_id
                        or memory["domain_profile"] != domain_profile
                        or memory["profile_version"] != profile_version
                    ):
                        raise SkillApiBlocked(
                            f"canonical write idempotency result is outside the requested scope: {row['idempotency_key']}"
                        )
                    automatic[row["idempotency_key"]] = WrittenMemoryRef(
                        memory_id=memory_id,
                        version=version,
                        request_sha256=row["request_sha256"],
                    )

    merged = dict(automatic)
    for key, explicit_ref in explicit.items():
        auto_ref = automatic.get(key)
        if auto_ref is not None:
            if auto_ref.memory_id != explicit_ref.memory_id or auto_ref.version != explicit_ref.version:
                raise SkillApiBlocked(
                    f"written_memory_map conflicts with the canonical idempotency mapping: {key}"
                )
            continue
        merged[key] = explicit_ref
    parsed = _parse_written_map(
        {key: ref.model_dump(mode="json") for key, ref in merged.items()}
    )
    entries = {
        key: {
            "memory_id": ref.memory_id,
            "version": ref.version,
            **({"request_sha256": ref.request_sha256} if ref.request_sha256 else {}),
        }
        for key, ref in sorted(parsed.items())
    }
    if automatic and explicit:
        source = f"sqlite+{explicit_source or 'override'}"
    elif automatic:
        source = "sqlite"
    elif explicit:
        source = explicit_source or "override"
    else:
        source = "sqlite"
    return entries, source


def apply_handoff_ingest_local(
    *,
    canonical_db: str | Path,
    handoff_dir: str | Path,
    handoff_id: str,
    workspace_id: str,
    project_id: str,
    domain_profile: str,
    profile_version: str,
    source_id: str,
    written_memory_map: Mapping[str, Any] | None,
    principal_id: str | None = None,
    owner_principal_id: str | None = None,
    ttl_seconds: int = 300,
    embedding_model: str = "fake-bge-m3-v1",
    embedding_dim: int = 1024,
    index_schema_version: str = "memory-index-v1",
) -> LocalHandoffApplyResult:
    """Apply a verified handoff using a short-lived local, auditable grant."""

    if (
        principal_id is not None
        and owner_principal_id is not None
        and principal_id != owner_principal_id
    ):
        raise SkillApiBlocked("principal_id and owner_principal_id conflict")
    effective_principal = principal_id if principal_id is not None else owner_principal_id
    if effective_principal is None or not effective_principal.strip():
        raise SkillApiBlocked("principal_id or owner_principal_id must be explicitly provided")
    if (
        isinstance(ttl_seconds, bool)
        or not isinstance(ttl_seconds, int)
        or not 1 <= ttl_seconds <= 3600
    ):
        raise SkillApiBlocked("ttl_seconds must be between 1 and 3600")
    for name, value in (
        ("handoff_id", handoff_id),
        ("workspace_id", workspace_id),
        ("project_id", project_id),
        ("domain_profile", domain_profile),
        ("profile_version", profile_version),
        ("source_id", source_id),
    ):
        if not value.strip():
            raise SkillApiBlocked(f"{name} must be non-empty")

    plan = plan_handoff_ingest(
        handoff_dir=handoff_dir,
        handoff_id=handoff_id,
        workspace_id=workspace_id,
        project_id=project_id,
        domain_profile=domain_profile,
        profile_version=profile_version,
        source_id=source_id,
        written_memory_map=written_memory_map,
    )
    profiles = ProfileRegistry()
    store = _validated_local_apply_store(
        canonical_db,
        workspace_id=workspace_id,
        project_id=project_id,
        domain_profile=domain_profile,
        profile_version=profile_version,
        profiles=profiles,
    )
    _preflight_local_delete_scope(
        store,
        plan.deletes,
        workspace_id=workspace_id,
        project_id=project_id,
        domain_profile=domain_profile,
        principal_id=effective_principal,
    )

    now = datetime.now(timezone.utc).replace(microsecond=0)
    grant = AccessGrant(
        grant_id="grant_local_handoff_" + uuid.uuid4().hex,
        principal_id=effective_principal,
        workspace_id=workspace_id,
        scope=AccessScope(
            project_ids=[project_id],
            domains=[domain_profile],
            max_sensitivity=SensitivityLevel.PRIVATE,
            allow_global_kb=False,
        ),
        actions=["write", "delete"],
        expires_at=now + timedelta(seconds=ttl_seconds),
        purpose=f"local_handoff_apply:{plan.plan_id}",
        created_at=now,
    )
    store.save_grant(grant)
    try:
        secret = secrets.token_bytes(32)
        token = issue_token(
            secret=secret,
            principal_id=effective_principal,
            task_id="local_handoff_apply:" + plan.plan_id,
            grant_id=grant.grant_id,
            workspace_id=workspace_id,
            ttl_seconds=ttl_seconds,
            now=now,
        )
        token_validator = TokenValidator(store, secret=secret)
        result = apply_handoff_ingest(
            handoff_dir=handoff_dir,
            handoff_id=handoff_id,
            workspace_id=workspace_id,
            project_id=project_id,
            domain_profile=domain_profile,
            profile_version=profile_version,
            source_id=source_id,
            written_memory_map=written_memory_map,
            store=store,
            profiles=profiles,
            token_validator=token_validator,
            token=token,
            embedding_model=embedding_model,
            embedding_dim=embedding_dim,
            index_schema_version=index_schema_version,
        )
    finally:
        store.save_grant(grant.model_copy(update={
            "revoked": True,
            "revoked_at": datetime.now(timezone.utc),
            "revoked_reason": "local_handoff_apply_finished",
        }))

    return LocalHandoffApplyResult(
        status=result.status,
        plan_id=result.plan_id,
        authorization=LocalHandoffAuthorizationReceipt(
            grant_id=grant.grant_id,
            principal_id=effective_principal,
            workspace_id=workspace_id,
            project_id=project_id,
            domain_profile=domain_profile,
            profile_version=profile_version,
            actions=("write", "delete"),
            expires_at=grant.expires_at,
        ),
        apply_result=result,
    )
