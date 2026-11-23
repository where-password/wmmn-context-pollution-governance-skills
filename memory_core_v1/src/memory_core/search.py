from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from .assertions import is_unverified_user_report
from .db import SQLiteStore, utc_now_ts
from .models import (
    REDACTED_SECRET_ANSWER, REDACTED_SECRET_QUERY, AccessScope,
    AgentContextToken, MemoryRecord, SearchHit, SearchRequest, SearchResult,
    SensitivityLevel, SynthesisCitation, SynthesisInputItem, SynthesisResult,
)
from .profiles import ProfileRegistry
from .security import TokenValidator, sensitivity_rank, status_allowed


class HybridSearchGateway:
    def __init__(
        self, *, store: SQLiteStore, profiles: ProfileRegistry,
        token_validator: TokenValidator, embedding: Any, vector_index: Any,
    ) -> None:
        self.store = store
        self.profiles = profiles
        self.tokens = token_validator
        self.embedding = embedding
        self.vector_index = vector_index

    def retrieve_context(
        self, token: AgentContextToken, request: SearchRequest,
        *, now: datetime | None = None, preauthorized_scope: AccessScope | None = None,
    ) -> list[SearchHit]:
        scope, hits = self._retrieve_hits(
            token, request, now=now, preauthorized_scope=preauthorized_scope
        )
        self._write_audit(
            token=token, request=request, scope=scope, hits=hits,
            answer_summary=f"[CONTEXT_ONLY] retrieved={len(hits)}",
        )
        return hits

    def search(
        self, token: AgentContextToken, request: SearchRequest,
        *, now: datetime | None = None, preauthorized_scope: AccessScope | None = None,
    ) -> SearchResult:
        scope, hits = self._retrieve_hits(
            token, request, now=now, preauthorized_scope=preauthorized_scope
        )
        synthesis = self._synthesize(hits, request.response_mode)
        self._write_audit(
            token=token, request=request, scope=scope, hits=hits,
            answer_summary=synthesis.answer[:500],
        )
        return SearchResult(hits=hits, synthesis=synthesis)

    def _retrieve_hits(
        self, token: AgentContextToken, request: SearchRequest,
        *, now: datetime | None = None, preauthorized_scope: AccessScope | None = None,
    ) -> tuple[AccessScope, list[SearchHit]]:
        now = now or datetime.now(timezone.utc)
        scope = preauthorized_scope or self.tokens.authorize_search(
            token, request, profiles=self.profiles, now=now
        )
        raw_chapters = request.payload_filters.get("chapter_index")
        if isinstance(raw_chapters, int):
            chapter_indices = [raw_chapters]
        elif isinstance(raw_chapters, list):
            chapter_indices = [int(value) for value in raw_chapters]
        else:
            chapter_indices = []
        candidate_limit = max(300, request.top_k * 3)
        fts_ids: list[str] = []
        query_text = request.query.strip()
        if re.fullmatch(r"[\u3400-\u9fff]{1,2}", query_text):
            fts_ids = [
                row["memory_id"] for row in self.store.substring_search(
                    query_text,
                    workspace_ids=(token.workspace_id,),
                    project_ids=tuple(scope.project_ids),
                    domains=tuple(scope.domains),
                    memory_types=tuple(scope.memory_types),
                    statuses=tuple(scope.statuses),
                    max_sensitivity=scope.max_sensitivity,
                    principal_id=token.principal_id,
                    allow_global_kb=scope.allow_global_kb,
                    limit=candidate_limit,
                    chapter_indices=chapter_indices,
                )
            ]
        elif len(query_text) >= 3:
            fts_ids = [
                row["memory_id"] for row in self.store.fts_search(
                    query_text, limit=candidate_limit, chapter_indices=chapter_indices
                )
            ]
        profile_id = scope.domains[0] if scope.domains else "novel"
        try:
            subject_profile = self.profiles.get(profile_id, "1.0.0")
        except KeyError:
            subject_profile = self.profiles.get("novel", "1.0.0")
        normalized_subject = subject_profile.normalize_subject(request.query, None)
        subject_ids = [
            row["memory_id"] for row in self.store.subject_search(
                normalized_subject, limit=candidate_limit, chapter_indices=chapter_indices
            )
        ] if normalized_subject else []
        query_vector = self.embedding.embed_one(request.query)
        if hasattr(self.vector_index, "search_ids"):
            vector_ids = [
                memory_id for memory_id, _ in self.vector_index.search_ids(
                    query_vector, filter=self._milvus_filter(token, scope), limit=candidate_limit
                )
            ]
        else:
            vector_results = self.vector_index.search(
                query_vector, limit=candidate_limit,
                predicate=lambda doc: self._index_doc_allowed(doc, token, scope),
            )
            vector_ids = [doc.memory_id for doc, _ in vector_results]

        rrf: dict[str, float] = defaultdict(float)
        seen: dict[str, set[str]] = defaultdict(set)
        for source_name, ids in (("fts", fts_ids), ("subject", subject_ids), ("dense", vector_ids)):
            for rank, memory_id in enumerate(ids, 1):
                if source_name in seen[memory_id]:
                    continue
                seen[memory_id].add(source_name)
                rrf[memory_id] += 1.0 / (60.0 + rank)

        hits: list[SearchHit] = []
        for memory_id, score in sorted(rrf.items(), key=lambda item: (-item[1], item[0])):
            record = self.store.get_memory_record(memory_id)
            if record is None or not self._record_allowed(record, token, scope):
                continue
            evidence_id = next((e.evidence_id for e in record.evidences if e.status.value == "verified"), None)
            hits.append(SearchHit(
                memory_id=record.memory_id, workspace_id=record.workspace_id,
                owner_principal_id=record.owner_principal_id, domain_profile=record.domain_profile,
                memory_type=record.memory_type, subtype=record.subtype, content=record.content,
                source_scope=record.source_scope, status=record.status,
                importance=record.importance, sensitivity=record.sensitivity,
                visibility=record.visibility,
                assertion_source=record.assertion_source,
                verification_state=record.verification_state,
                origin="global_kb" if record.workspace_id == "global_kb" else "workspace",
                evidence_id=evidence_id, score=round(score, 8),
            ))
            if len(hits) >= request.top_k:
                break
        return scope, hits

    def _write_audit(
        self, *, token: AgentContextToken, request: SearchRequest,
        scope: AccessScope, hits: list[SearchHit], answer_summary: str,
    ) -> None:
        secret_result = (
            scope.max_sensitivity == SensitivityLevel.SECRET
            or any(hit.sensitivity == SensitivityLevel.SECRET for hit in hits)
        )
        self.store.insert_query_audit({
            "query_id": f"qa_{uuid.uuid4().hex}", "principal_id": token.principal_id,
            "token_jti": token.jti, "grant_id": token.grant_id,
            "workspace_id": token.workspace_id,
            "query_hash": hashlib.sha256(request.query.encode("utf-8")).hexdigest(),
            "redacted_query": REDACTED_SECRET_QUERY if secret_result else request.query,
            "max_sensitivity": scope.max_sensitivity.value,
            "filters_json": scope.model_dump(mode="json"),
            "retrieved_ids_json": [hit.memory_id for hit in hits],
            "answer_summary": REDACTED_SECRET_ANSWER if secret_result else answer_summary,
            "created_at": utc_now_ts(),
        })

    @staticmethod
    def _sql_literal(value: str) -> str:
        return json.dumps(str(value), ensure_ascii=False)

    def _milvus_filter(self, token: AgentContextToken, scope: AccessScope) -> str:
        deny = "__deny_all__" in {*scope.project_ids, *scope.domains, *scope.memory_types}
        if deny:
            return 'memory_id == "__no_access__"'
        workspaces = [token.workspace_id]
        if scope.allow_global_kb:
            workspaces.append("global_kb")
        conditions = [
            f"workspace_id in [{','.join(self._sql_literal(x) for x in workspaces)}]",
            f"status in [{','.join(self._sql_literal(x.value) for x in scope.statuses)}]",
        ]
        allowed_sensitivities = [
            level.value for level in SensitivityLevel
            if sensitivity_rank(level) <= sensitivity_rank(scope.max_sensitivity)
        ]
        conditions.append(
            f"sensitivity in [{','.join(self._sql_literal(x) for x in allowed_sensitivities)}]"
        )
        conditions.append(
            f'(visibility != "private" or owner_principal_id == {self._sql_literal(token.principal_id)})'
        )
        if scope.project_ids:
            conditions.append(f"project_id in [{','.join(self._sql_literal(x) for x in scope.project_ids)}]")
        if scope.domains:
            conditions.append(f"domain_profile in [{','.join(self._sql_literal(x) for x in scope.domains)}]")
        if scope.memory_types:
            conditions.append(f"memory_type in [{','.join(self._sql_literal(x) for x in scope.memory_types)}]")
        raw_chapters = scope.payload_filters.get("chapter_index")
        if isinstance(raw_chapters, int):
            chapters = [raw_chapters]
        elif isinstance(raw_chapters, list):
            chapters = [int(value) for value in raw_chapters]
        else:
            chapters = []
        if chapters:
            conditions.append(
                "indexed_payload_json[\"chapter_index\"] in "
                f"[{','.join(str(x) for x in chapters)}]"
            )
        return " and ".join(conditions)

    def _index_doc_allowed(self, doc, token: AgentContextToken, scope: AccessScope) -> bool:
        if doc.workspace_id != token.workspace_id and not (scope.allow_global_kb and doc.workspace_id == "global_kb"):
            return False
        if doc.visibility == "private" and doc.owner_principal_id != token.principal_id:
            return False
        if sensitivity_rank(doc.sensitivity) > sensitivity_rank(scope.max_sensitivity):
            return False
        if scope.project_ids and doc.project_id not in scope.project_ids:
            return False
        if scope.domains and doc.domain_profile not in scope.domains:
            return False
        if scope.memory_types and doc.memory_type not in scope.memory_types:
            return False
        return doc.status in scope.statuses

    def _record_allowed(self, record: MemoryRecord, token: AgentContextToken, scope: AccessScope) -> bool:
        if record.deleted_at is not None:
            return False
        if record.workspace_id != token.workspace_id and not (scope.allow_global_kb and record.workspace_id == "global_kb"):
            return False
        if record.visibility == "private" and record.owner_principal_id != token.principal_id:
            return False
        if sensitivity_rank(record.sensitivity) > sensitivity_rank(scope.max_sensitivity):
            return False
        if not status_allowed(record.status, scope.statuses):
            return False
        if scope.project_ids and record.project_id not in scope.project_ids:
            return False
        if scope.domains and record.domain_profile not in scope.domains:
            return False
        if scope.memory_types and record.memory_type not in scope.memory_types:
            return False
        for key, expected in scope.payload_filters.items():
            actual = record.domain_payload.get(key)
            if isinstance(expected, list):
                if actual not in expected:
                    return False
            elif actual != expected:
                return False
        return True

    @staticmethod
    def _synthesize(hits: list[SearchHit], mode: str) -> SynthesisResult:
        audit = mode == "audit"
        items = []
        unverified = []
        for hit in hits[:10]:
            is_unverified = is_unverified_user_report(hit.assertion_source, hit.verification_state)
            content = f"用户陈述（未外部验证）：{hit.content}" if is_unverified else hit.content
            if is_unverified:
                unverified.append(f"{hit.memory_id} 为用户陈述，尚未获得外部事实验证。")
            items.append(SynthesisInputItem(
                content=content, source_label=hit.source_scope.ref,
                source_scope=hit.source_scope, origin=hit.origin,
                assertion_source=hit.assertion_source, verification_state=hit.verification_state,
            ))
        answer = "；".join(item.content for item in items) if items else "未检索到可用记忆。"
        citations = [SynthesisCitation(
            source_label=hit.source_scope.ref, source_scope=hit.source_scope,
            origin=hit.origin, memory_ids=[hit.memory_id] if audit else [],
        ) for hit in hits[:10]]
        origins = {hit.origin for hit in hits}
        if origins == {"workspace"}:
            origin = "workspace"
        elif origins == {"global_kb"}:
            origin = "global_kb"
        elif origins:
            origin = "mixed"
        else:
            origin = "workspace"
        return SynthesisResult(
            answer=answer, mode=mode, citations=citations, uncertainties=unverified, conflicts=[],
            origin=origin, includes_internal_ids=audit,
        )
