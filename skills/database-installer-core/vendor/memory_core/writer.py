from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from .assertions import validate_assertion_policy
from .db import SQLiteStore, dt_to_ts, json_dumps, utc_now_ts
from .models import (
    AgentContextToken,
    EvidenceStatus,
    MemoryEvidence,
    MemoryStatus,
    MemoryWriteInput,
    MemoryWriteResult,
    SensitivityLevel,
    max_sensitivity,
)
from .profiles import ProfileRegistry
from .security import TokenValidator


class MemoryWriteError(RuntimeError):
    pass


class IdempotencyConflict(MemoryWriteError):
    pass


class SourceAccessDenied(MemoryWriteError):
    pass


class EvidencePolicyError(MemoryWriteError):
    pass


class MemoryWriter:
    def __init__(
        self, *, store: SQLiteStore, profiles: ProfileRegistry, token_validator: TokenValidator,
        embedding_model: str = "fake-bge-m3-v1", embedding_dim: int = 1024,
        index_schema_version: str = "memory-index-v1",
    ) -> None:
        self.store = store
        self.profiles = profiles
        self.tokens = token_validator
        self.embedding_model = embedding_model
        self.embedding_dim = embedding_dim
        self.index_schema_version = index_schema_version

    @staticmethod
    def _request_hash(item: MemoryWriteInput) -> str:
        payload = json.dumps(item.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def _content_hash(*, domain_profile: str, normalized_subject: str, normalized_content: str) -> str:
        return hashlib.sha256(f"v1|{domain_profile}|{normalized_subject}|{normalized_content}".encode("utf-8")).hexdigest()

    def _authorize(self, item: MemoryWriteInput, token: AgentContextToken) -> None:
        context = self.tokens.validate(token, action="write", consume_query=False)
        grant = context.grant
        if token.workspace_id != item.workspace_id or grant.workspace_id != item.workspace_id:
            raise SourceAccessDenied("workspace mismatch")
        if grant.scope.project_ids and item.project_id not in grant.scope.project_ids:
            raise SourceAccessDenied("project is outside grant scope")
        if grant.scope.domains and item.domain_profile not in grant.scope.domains:
            raise SourceAccessDenied("domain is outside grant scope")
        if grant.scope.memory_types and item.memory_type not in grant.scope.memory_types:
            raise SourceAccessDenied("memory_type is outside grant scope")

    def _validate_source(self, item: MemoryWriteInput, token: AgentContextToken) -> tuple[dict[str, Any], dict[str, Any], str, str, SensitivityLevel, str]:
        source = self.store.get_source(item.source_id)
        unit = self.store.get_source_unit(item.source_unit_id)
        if source is None or unit is None:
            raise SourceAccessDenied("source or source unit not found")
        if source["workspace_id"] != item.workspace_id or source["project_id"] != item.project_id:
            raise SourceAccessDenied("source scope does not match memory workspace/project")
        if unit["source_id"] != item.source_id:
            raise SourceAccessDenied("source unit does not belong to source")
        if source["visibility"] == "private":
            if source["owner_principal_id"] != token.principal_id:
                raise SourceAccessDenied("private source belongs to another principal")
            owner = source["owner_principal_id"]
            visibility = "private"
        else:
            owner = token.principal_id
            visibility = item.requested_visibility
        sensitivity = max_sensitivity(source["sensitivity"], item.declared_sensitivity)
        return source, unit, owner, visibility, sensitivity, unit["source_unit_hash"]

    def write(self, item: MemoryWriteInput, token: AgentContextToken) -> MemoryWriteResult:
        self._authorize(item, token)
        profile = self.profiles.get(item.domain_profile, item.profile_version)
        profile.validate_item(item)
        source, unit, owner, visibility, sensitivity, source_unit_hash = self._validate_source(item, token)
        if not source_unit_hash:
            raise EvidencePolicyError("source unit hash missing")

        request_hash = self._request_hash(item)
        existing_idem = self.store.get_idempotency(item.workspace_id, token.principal_id, item.idempotency_key)
        if existing_idem:
            if existing_idem["request_sha256"] != request_hash:
                raise IdempotencyConflict("idempotency key was reused with a different request")
            return MemoryWriteResult.model_validate_json(existing_idem["result_json"])

        normalized_subject = profile.normalize_subject(item.subject, item.entity_type)
        normalized_content = profile.normalize_content(item.content)
        content_hash = self._content_hash(
            domain_profile=item.domain_profile, normalized_subject=normalized_subject,
            normalized_content=normalized_content,
        )
        dedup_policy = profile.default_dedup_policy(item.memory_type, item.subtype)
        evidence_models: list[MemoryEvidence] = []
        for evidence in item.evidences:
            evidence_unit = self.store.get_source_unit(evidence.source_unit_id)
            if evidence_unit is None or evidence_unit["source_id"] != item.source_id:
                raise EvidencePolicyError("every evidence source unit must belong to the memory source")
            evidence_models.append(evidence)
        has_verified = any(e.status == EvidenceStatus.VERIFIED for e in evidence_models)
        target_status = MemoryStatus.ACTIVE if has_verified else MemoryStatus.PENDING_REVIEW
        assertion_validator = getattr(profile, "validate_assertion", None)
        if assertion_validator is not None:
            assertion_validator(item, target_status)
        else:
            validate_assertion_policy(
                assertion_source=item.assertion_source,
                verification_state=item.verification_state,
                status=target_status,
            )

        now = utc_now_ts()
        memory_id = f"mem_{uuid.uuid4().hex}"
        version = 1
        outbox_ids: list[str] = []
        with self.store.transaction() as connection:
            idem = connection.execute(
                """SELECT * FROM idempotency_records
                   WHERE workspace_id=? AND principal_id=? AND idempotency_key=?""",
                (item.workspace_id, token.principal_id, item.idempotency_key),
            ).fetchone()
            if idem:
                if idem["request_sha256"] != request_hash:
                    raise IdempotencyConflict("idempotency key was reused with a different request")
                return MemoryWriteResult.model_validate_json(idem["result_json"])

            duplicate = self.store.find_active_content(
                workspace_id=item.workspace_id, owner_principal_id=owner,
                domain_profile=item.domain_profile, content_hash=content_hash,
                normalized_subject=normalized_subject, visibility=visibility, connection=connection,
            )
            if duplicate:
                result = MemoryWriteResult(
                    memory_id=duplicate["memory_id"], status=MemoryStatus(duplicate["status"]),
                    version=int(duplicate["version"]), created=False, deduplicated=True,
                )
                self.store.insert_idempotency(
                    workspace_id=item.workspace_id, principal_id=token.principal_id,
                    idempotency_key=item.idempotency_key, operation_type="write",
                    request_sha256=request_hash, operation_id=item.operation_id,
                    result=result.model_dump(mode="json"), status="deduplicated", connection=connection,
                )
                return result

            supersedes_id = item.supersedes_id
            if supersedes_id:
                seen: set[str] = set()
                current_id: str | None = supersedes_id
                depth = 0
                while current_id:
                    if current_id in seen or depth >= 50:
                        raise MemoryWriteError("supersedes cycle or depth exceeded")
                    seen.add(current_id)
                    row = connection.execute(
                        "SELECT * FROM memory_items WHERE memory_id=? AND workspace_id=?",
                        (current_id, item.workspace_id),
                    ).fetchone()
                    if not row:
                        raise MemoryWriteError("supersedes target not found")
                    if row["visibility"] == "private" and row["owner_principal_id"] != owner:
                        raise SourceAccessDenied("cannot supersede another owner's private memory")
                    current_id = row["supersedes_id"]
                    depth += 1
                target = connection.execute("SELECT * FROM memory_items WHERE memory_id=?", (supersedes_id,)).fetchone()
                if target["status"] != MemoryStatus.ACTIVE.value or target["deleted_at"] is not None:
                    raise MemoryWriteError("supersedes target is not active")
                if item.expected_version is not None and int(target["version"]) != item.expected_version:
                    raise MemoryWriteError("optimistic version check failed")
                version = int(target["version"]) + 1
                connection.execute(
                    "UPDATE memory_items SET status='superseded',updated_at=? WHERE memory_id=?",
                    (now, supersedes_id),
                )
                superseded_targets = [("fts", "memory_fts", None, None)]
                if target["sensitivity"] != SensitivityLevel.SECRET.value:
                    superseded_targets.append(
                        ("milvus", "memory_write_alias", self.embedding_model, self.embedding_dim)
                    )
                for target_type, target_name, model, dim in superseded_targets:
                    outbox_id = "out_" + hashlib.sha256(
                        f"{supersedes_id}|{target_type}|{target_name}|{int(target['version'])}|"
                        f"{self.index_schema_version}|delete".encode("utf-8")
                    ).hexdigest()[:32]
                    if self.store.insert_outbox(
                        outbox_id=outbox_id, memory_id=supersedes_id,
                        workspace_id=item.workspace_id, target_type=target_type,
                        target_name=target_name, operation="delete",
                        item_version=int(target["version"]),
                        index_schema_version=self.index_schema_version,
                        embedding_model=model, embedding_dim=dim,
                        connection=connection,
                    ):
                        outbox_ids.append(outbox_id)

            memory_values: dict[str, Any] = {
                "memory_id": memory_id, "workspace_id": item.workspace_id,
                "project_id": item.project_id, "task_id": item.task_id,
                "conversation_id": item.conversation_id, "owner_principal_id": owner,
                "domain_profile": item.domain_profile, "profile_version": item.profile_version,
                "memory_type": item.memory_type, "subtype": item.subtype, "subject": item.subject,
                "normalized_subject": normalized_subject, "entity_type": item.entity_type,
                "entity_id": item.entity_id, "content": item.content, "content_hash": content_hash,
                "content_hash_version": "v1", "dedup_policy": dedup_policy.value,
                "status": MemoryStatus.PENDING_REVIEW.value, "importance": item.importance,
                "sensitivity": sensitivity.value, "visibility": visibility,
                "assertion_source": item.assertion_source.value,
                "verification_state": item.verification_state.value,
                "effective_from": dt_to_ts(item.effective_from), "effective_to": dt_to_ts(item.effective_to),
                "source_id": item.source_id, "source_unit_id": item.source_unit_id,
                "source_scope_json": json_dumps(item.source_scope.model_dump(mode="json")),
                "confidence": item.confidence, "supersedes_id": supersedes_id, "version": version,
                "domain_payload_json": json_dumps(profile.validate_payload(item.subtype, item.domain_payload)),
                "idempotency_key": item.idempotency_key, "deleted_at": None,
                "delete_reason": None, "created_at": now, "updated_at": now,
            }
            self.store.insert_memory_pending(memory_values, connection=connection)
            evidence_ids: list[str] = []
            for evidence in evidence_models:
                normalized_evidence = evidence.model_copy(
                    update={"evidence_id": evidence.evidence_id or f"ev_{uuid.uuid4().hex}"}
                )
                self.store.insert_evidence(memory_id, normalized_evidence, connection=connection)
                evidence_ids.append(normalized_evidence.evidence_id)
            if target_status == MemoryStatus.ACTIVE:
                self.store.activate_memory(memory_id, connection=connection)
                targets = [("fts", "memory_fts", None, None)]
                if sensitivity != SensitivityLevel.SECRET:
                    targets.append(("milvus", "memory_write_alias", self.embedding_model, self.embedding_dim))
                for target_type, target_name, model, dim in targets:
                    outbox_id = "out_" + hashlib.sha256(
                        f"{memory_id}|{target_type}|{target_name}|{version}|{self.index_schema_version}|upsert".encode("utf-8")
                    ).hexdigest()[:32]
                    if self.store.insert_outbox(
                        outbox_id=outbox_id, memory_id=memory_id, workspace_id=item.workspace_id,
                        target_type=target_type, target_name=target_name, operation="upsert",
                        item_version=version, index_schema_version=self.index_schema_version,
                        embedding_model=model, embedding_dim=dim, connection=connection,
                    ):
                        outbox_ids.append(outbox_id)

            result = MemoryWriteResult(
                memory_id=memory_id, status=target_status, version=version, created=True,
                deduplicated=False, evidence_ids=evidence_ids, outbox_ids=outbox_ids,
            )
            self.store.insert_idempotency(
                workspace_id=item.workspace_id, principal_id=token.principal_id,
                idempotency_key=item.idempotency_key, operation_type="write",
                request_sha256=request_hash, operation_id=item.operation_id,
                result=result.model_dump(mode="json"), status="completed", connection=connection,
            )
            return result
