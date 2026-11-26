from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ..assertions import is_unverified_user_report, validate_assertion_policy
from ..db import MIGRATIONS_DIR
from ..models import (
    AssertionSource,
    MemoryStatus,
    SENSITIVITY_ORDER,
    SensitivityLevel,
    SourceScope,
    VerificationState,
)
from ..profiles import ProfileRegistry
from .attach import connection_profile_to_database_profile, load_database_profile
from .installer import ConnectionProfile
from .models import DatabaseProfile, SkillApiBlocked, SkillBackend


class QueryHit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    content: str
    citation_label: str
    memory_type: str
    subtype: str | None = None
    importance: str
    sensitivity: SensitivityLevel
    visibility: str
    assertion_source: AssertionSource
    verification_state: VerificationState
    source_scope: SourceScope
    score: float = 0.0
    unverified_user_report: bool = False
    memory_id: str = Field(exclude=True)


class MemorySearchResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: Literal["search_only"] = "search_only"
    backend: SkillBackend = SkillBackend.SQLITE
    query: str
    evidence_completeness: Literal["not_assessed", "partial", "no_evidence"] = "not_assessed"
    hits: list[QueryHit] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    scope: dict[str, Any] = Field(default_factory=dict)


class MemoryEvidenceView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    citation_label: str
    status: str
    locator_json: dict[str, Any] = Field(default_factory=dict)
    quote: str | None = None


class MemoryDetailResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: Literal["memory_detail"] = "memory_detail"
    backend: SkillBackend = SkillBackend.SQLITE
    memory_id: str
    version: int
    content: str
    memory_type: str
    subtype: str | None = None
    subject: str | None = None
    importance: str
    sensitivity: SensitivityLevel
    visibility: str
    assertion_source: AssertionSource
    verification_state: VerificationState
    source_scope: SourceScope
    unverified_user_report: bool = False
    evidences: list[MemoryEvidenceView] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    scope: dict[str, Any] = Field(default_factory=dict)


class QueryContextResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: Literal["context_only"] = "context_only"
    backend: SkillBackend = SkillBackend.SQLITE
    query: str
    evidence_completeness: Literal["not_assessed", "partial", "no_evidence"] = "not_assessed"
    hits: list[QueryHit] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    scope: dict[str, Any] = Field(default_factory=dict)


class ContextPackEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    category: Literal["current", "conflict", "history"]
    content: str
    citation_label: str
    memory_type: str
    subtype: str | None = None
    importance: str
    sensitivity: SensitivityLevel
    visibility: str
    assertion_source: AssertionSource
    verification_state: VerificationState
    source_scope: SourceScope
    evidence_status: Literal["verified", "unverified"] = "unverified"
    unverified_user_report: bool = False
    version: int
    claim_key: str | None = None


class ContextSourceRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    citation_label: str
    verification_state: VerificationState
    unverified_user_report: bool = False


class ContextPackResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: Literal["context_pack"] = "context_pack"
    contract_version: Literal["skill-context-pack-v1"] = "skill-context-pack-v1"
    backend: SkillBackend = SkillBackend.SQLITE
    query: str
    token_budget: int
    estimated_tokens: int
    truncated: bool = False
    evidence_completeness: Literal["not_assessed", "partial", "no_evidence"] = "not_assessed"
    current: list[ContextPackEntry] = Field(default_factory=list)
    conflicts: list[ContextPackEntry] = Field(default_factory=list)
    history: list[ContextPackEntry] = Field(default_factory=list)
    source_refs: list[ContextSourceRef] = Field(default_factory=list)
    omitted_counts: dict[str, int] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    scope: dict[str, Any] = Field(default_factory=dict)


class QueryAnswerResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: Literal["extractive"] = "extractive"
    backend: SkillBackend = SkillBackend.SQLITE
    query: str
    answer: str
    points: list[str] = Field(default_factory=list)
    source_ranges: list[str] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    evidence_completeness: Literal["not_assessed", "partial", "no_evidence"] = "not_assessed"
    synthesized: bool = False
    scope: dict[str, Any] = Field(default_factory=dict)


class HealthResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: Literal["health"] = "health"
    backend: SkillBackend = SkillBackend.SQLITE
    status: Literal["ok", "warning", "critical", "blocked"] = "warning"
    consistency_status: Literal["ok", "warning", "critical", "not_assessed"] = "not_assessed"
    quality_status: Literal["ok", "warning", "critical", "not_assessed"] = "not_assessed"
    freshness_status: Literal["current", "warning", "stale", "unknown"] = "unknown"
    checks: dict[str, bool] = Field(default_factory=dict)
    counts: dict[str, int] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    scope: dict[str, Any] = Field(default_factory=dict)


def _profile(value: ConnectionProfile | DatabaseProfile | str | Path) -> DatabaseProfile:
    if isinstance(value, DatabaseProfile):
        return value
    if isinstance(value, ConnectionProfile):
        return connection_profile_to_database_profile(value)
    return load_database_profile(value)


def _open_readonly(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=30000")
    return connection


@contextmanager
def _readonly_connection(path: Path):
    connection = _open_readonly(path)
    try:
        yield connection
    finally:
        connection.close()


def _project(profile: DatabaseProfile, project_id: str | None) -> str:
    if project_id:
        if project_id not in profile.project_ids:
            raise SkillApiBlocked("project is outside the attached profile scope")
        return project_id
    if len(profile.project_ids) == 1:
        return profile.project_ids[0]
    raise SkillApiBlocked("project_id is required when the profile has multiple projects")


def _max_sensitivity(profile: DatabaseProfile, requested: str | None) -> SensitivityLevel:
    profile_level = profile.max_sensitivity
    if requested is None:
        return profile_level
    requested_level = SensitivityLevel(requested)
    return profile_level if SENSITIVITY_ORDER[profile_level] <= SENSITIVITY_ORDER[requested_level] else requested_level


def _allowed_sensitivities(level: SensitivityLevel) -> list[str]:
    limit = SENSITIVITY_ORDER[level]
    return [item.value for item in SensitivityLevel if SENSITIVITY_ORDER[item] <= limit]


def _visibility_values(profile: DatabaseProfile) -> tuple[str, ...]:
    allowed = {"private", "workspace", "shared"}
    values = tuple(dict.fromkeys(profile.allowed_visibilities))
    if not values or not set(values).issubset(allowed):
        raise SkillApiBlocked("profile allowed_visibilities contains unsupported values")
    return values


def _scope(profile: DatabaseProfile, project_id: str, sensitivity: SensitivityLevel) -> tuple[str, list[Any]]:
    allowed = _allowed_sensitivities(sensitivity)
    sensitivity_placeholders = ",".join("?" for _ in allowed)
    visibilities = _visibility_values(profile)
    visibility_placeholders = ",".join("?" for _ in visibilities)
    owner = profile.owner_principal_id or "__no_private_owner__"
    clause = (
        "m.workspace_id=? AND m.project_id=? AND m.domain_profile=? "
        "AND m.status='active' AND m.deleted_at IS NULL "
        f"AND m.sensitivity IN ({sensitivity_placeholders}) "
        f"AND m.visibility IN ({visibility_placeholders}) "
        "AND (m.visibility != 'private' OR m.owner_principal_id=?)"
    )
    return clause, [profile.workspace_id, project_id, profile.domain_profile, *allowed, *visibilities, owner]


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _row_to_hit(row: sqlite3.Row, *, max_content_chars: int, score: float = 0.0) -> QueryHit:
    source_scope = SourceScope.model_validate(json.loads(row["source_scope_json"]))
    assertion_source = AssertionSource(row["assertion_source"])
    verification_state = VerificationState(row["verification_state"])
    return QueryHit(
        content=row["content"][:max_content_chars],
        citation_label=source_scope.ref or source_scope.scope_type,
        memory_type=row["memory_type"],
        subtype=row["subtype"],
        importance=row["importance"],
        sensitivity=SensitivityLevel(row["sensitivity"]),
        visibility=row["visibility"],
        assertion_source=assertion_source,
        verification_state=verification_state,
        source_scope=source_scope,
        score=score,
        unverified_user_report=is_unverified_user_report(assertion_source, verification_state),
        memory_id=row["memory_id"],
    )


def _milvus_literal(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _milvus_filter(profile: DatabaseProfile, project: str, sensitivity: SensitivityLevel) -> str:
    allowed = _allowed_sensitivities(sensitivity)
    visibilities = _visibility_values(profile)
    owner = profile.owner_principal_id or "__no_private_owner__"
    sensitivity_values = ",".join(f'"{_milvus_literal(value)}"' for value in allowed)
    visibility_values = ",".join(f'"{_milvus_literal(value)}"' for value in visibilities)
    return " and ".join([
        f'workspace_id == "{_milvus_literal(profile.workspace_id)}"',
        f'project_id == "{_milvus_literal(project)}"',
        f'domain_profile == "{_milvus_literal(profile.domain_profile)}"',
        'status == "active"',
        f'sensitivity in [{sensitivity_values}]',
        f'visibility in [{visibility_values}]',
        f'(visibility != "private" or owner_principal_id == "{_milvus_literal(owner)}")',
    ])


def _fetch_hits_by_ids(connection: sqlite3.Connection, ids: list[str], scope_clause: str, scope_params: list[Any]) -> list[sqlite3.Row]:
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    return list(connection.execute(
        f"""SELECT m.memory_id,m.content,m.memory_type,m.subtype,m.importance,m.sensitivity,
                   m.visibility,m.assertion_source,m.verification_state,m.source_scope_json,
                   0.0 AS rank_score
            FROM memory_items m
            WHERE m.memory_id IN ({placeholders}) AND {scope_clause}
            ORDER BY m.updated_at DESC""",
        [*ids, *scope_params],
    ))


def query_context(
    profile: ConnectionProfile | DatabaseProfile | str | Path,
    *,
    query: str,
    search_query: str | None = None,
    project_id: str | None = None,
    top_k: int = 10,
    max_sensitivity: str | None = None,
    max_content_chars: int = 2000,
    read_index: Any | None = None,
    embedding: Any | None = None,
) -> QueryContextResult:
    profile_value = _profile(profile)
    if "query" not in profile_value.capabilities:
        raise SkillApiBlocked("profile capabilities do not allow query")
    display_query = query.strip()
    text = (search_query or query).strip()
    if not text:
        raise SkillApiBlocked("query must not be empty")
    if not 1 <= top_k <= 100:
        raise SkillApiBlocked("top_k must be between 1 and 100")
    if not 100 <= max_content_chars <= 4000:
        raise SkillApiBlocked("max_content_chars must be between 100 and 4000")
    project = _project(profile_value, project_id)
    sensitivity = _max_sensitivity(profile_value, max_sensitivity)
    scope_clause, scope_params = _scope(profile_value, project, sensitivity)
    canonical = profile_value.canonical_db.resolve()
    if not canonical.is_file():
        raise SkillApiBlocked("canonical database does not exist")
    if profile_value.backend == SkillBackend.MILVUS:
        if read_index is None or embedding is None:
            raise SkillApiBlocked("milvus query requires a read-only index and embedding client")
        if not profile_value.read_alias:
            raise SkillApiBlocked("milvus profile does not contain a read alias")

    rows: list[sqlite3.Row] = []
    warnings: list[str] = []
    with _readonly_connection(canonical) as connection:
        table_names = {
            row["name"] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
            )
        }
        if "memory_fts" not in table_names or "memory_items" not in table_names:
            raise SkillApiBlocked("canonical database does not expose FTS and memory_items")
        if len(text) >= 3:
            term = search_query.strip() if search_query is not None else ('"' + text.replace('"', '""') + '"')
            rows.extend(connection.execute(
                f"""SELECT m.memory_id,m.content,m.memory_type,m.subtype,m.importance,m.sensitivity,
                           m.visibility,m.assertion_source,m.verification_state,m.source_scope_json,
                           bm25(memory_fts) AS rank_score
                    FROM memory_fts f JOIN memory_items m ON m.memory_id=f.memory_id
                    WHERE memory_fts MATCH ? AND {scope_clause}
                    ORDER BY rank LIMIT ?""",
                [term, *scope_params, top_k],
            ).fetchall())
        else:
            warnings.append("query shorter than 3 characters; using bounded substring fallback")
            like_text = f"%{text}%"
            rows.extend(connection.execute(
                f"""SELECT m.memory_id,m.content,m.memory_type,m.subtype,m.importance,m.sensitivity,
                           m.visibility,m.assertion_source,m.verification_state,m.source_scope_json,
                           0.0 AS rank_score
                    FROM memory_items m
                    WHERE (m.content LIKE ? OR m.normalized_subject LIKE ?) AND {scope_clause}
                    ORDER BY m.updated_at DESC LIMIT ?""",
                [like_text, like_text, *scope_params, top_k],
            ).fetchall())

        try:
            domain_profile = ProfileRegistry().get(profile_value.domain_profile, "1.0.0")
        except KeyError:
            domain_profile = None
            warnings.append("domain profile normalization is unavailable; subject search skipped")
        if domain_profile is not None:
            normalized_subject = domain_profile.normalize_subject(text, None)
            if normalized_subject:
                rows.extend(connection.execute(
                    f"""SELECT m.memory_id,m.content,m.memory_type,m.subtype,m.importance,m.sensitivity,
                               m.visibility,m.assertion_source,m.verification_state,m.source_scope_json,
                               0.0 AS rank_score
                        FROM memory_items m
                        WHERE m.normalized_subject=? AND {scope_clause}
                        ORDER BY m.updated_at DESC LIMIT ?""",
                    [normalized_subject, *scope_params, top_k],
                ).fetchall())

        if profile_value.backend == SkillBackend.MILVUS:
            try:
                query_vector = embedding.embed_one(text)
                dense_ids = [
                    memory_id for memory_id, _ in read_index.search_ids(
                        query_vector,
                        filter=_milvus_filter(profile_value, project, sensitivity),
                        limit=max(top_k * 3, 10),
                    )
                ]
            except Exception as exc:
                raise SkillApiBlocked(f"milvus query adapter failed: {type(exc).__name__}") from exc
            rows.extend(_fetch_hits_by_ids(connection, dense_ids, scope_clause, scope_params))

    unique: dict[str, sqlite3.Row] = {}
    for row in rows:
        unique.setdefault(row["memory_id"], row)
    hits = [
        _row_to_hit(row, max_content_chars=max_content_chars, score=float(row["rank_score"] or 0.0))
        for row in list(unique.values())[:top_k]
    ]
    if len(hits) >= top_k:
        warnings.append("result limit reached; recall may be incomplete")
    completeness: Literal["not_assessed", "partial", "no_evidence"] = "no_evidence" if not hits else "partial"
    return QueryContextResult(
        backend=profile_value.backend,
        query=display_query,
        evidence_completeness=completeness,
        hits=hits,
        warnings=warnings,
        scope={
            "workspace_id": profile_value.workspace_id,
            "project_id": project,
            "domain_profile": profile_value.domain_profile,
            "max_sensitivity": sensitivity.value,
        },
    )


def search_memory(
    profile: ConnectionProfile | DatabaseProfile | str | Path,
    *,
    query: str,
    project_id: str | None = None,
    top_k: int = 10,
    max_sensitivity: str | None = None,
    max_content_chars: int = 2000,
    read_index: Any | None = None,
    embedding: Any | None = None,
) -> MemorySearchResult:
    """Return scoped ranked hits only; the caller's LLM owns final synthesis."""

    context = query_context(
        profile,
        query=query,
        project_id=project_id,
        top_k=top_k,
        max_sensitivity=max_sensitivity,
        max_content_chars=max_content_chars,
        read_index=read_index,
        embedding=embedding,
    )
    return MemorySearchResult(
        backend=context.backend,
        query=context.query,
        evidence_completeness=context.evidence_completeness,
        hits=context.hits,
        warnings=context.warnings,
        scope=context.scope,
    )


def _estimate_context_tokens(text: str) -> int:
    cjk = 0
    other = 0
    for character in text:
        if ord(character) >= 0x2E80:
            cjk += 1
        else:
            other += 1
    return max(1, cjk + (other + 3) // 4)


def _context_scope(
    profile: DatabaseProfile,
    project_id: str,
    sensitivity: SensitivityLevel,
    statuses: tuple[str, ...],
) -> tuple[str, list[Any]]:
    allowed = _allowed_sensitivities(sensitivity)
    visibilities = _visibility_values(profile)
    owner = profile.owner_principal_id or "__no_private_owner__"
    sensitivity_placeholders = ",".join("?" for _ in allowed)
    visibility_placeholders = ",".join("?" for _ in visibilities)
    status_placeholders = ",".join("?" for _ in statuses)
    clause = (
        "m.workspace_id=? AND m.project_id=? AND m.domain_profile=? "
        "AND m.deleted_at IS NULL "
        f"AND m.status IN ({status_placeholders}) "
        f"AND m.sensitivity IN ({sensitivity_placeholders}) "
        f"AND m.visibility IN ({visibility_placeholders}) "
        "AND (m.visibility != 'private' OR m.owner_principal_id=?)"
    )
    return clause, [
        profile.workspace_id,
        project_id,
        profile.domain_profile,
        *statuses,
        *allowed,
        *visibilities,
        owner,
    ]


def _context_row(
    connection: sqlite3.Connection,
    clause: str,
    params: list[Any],
    *,
    memory_id: str,
) -> sqlite3.Row | None:
    return connection.execute(
        f"""SELECT m.memory_id,m.version,m.content,m.memory_type,m.subtype,m.importance,
                   m.sensitivity,m.visibility,m.assertion_source,m.verification_state,
                   m.source_scope_json,m.domain_payload_json,m.supersedes_id,m.status,
                   CASE WHEN EXISTS (
                     SELECT 1 FROM memory_evidence e
                     WHERE e.memory_id=m.memory_id AND e.evidence_status='verified'
                   ) THEN 'verified' ELSE 'unverified' END AS evidence_status
            FROM memory_items m
            WHERE m.memory_id=? AND {clause}""",
        [memory_id, *params],
    ).fetchone()


def _pack_entry_from_row(row: sqlite3.Row, category: Literal["current", "conflict", "history"]) -> ContextPackEntry:
    source_scope = SourceScope.model_validate(json.loads(row["source_scope_json"]))
    assertion_source = AssertionSource(row["assertion_source"])
    verification_state = VerificationState(row["verification_state"])
    payload = json.loads(row["domain_payload_json"])
    handoff = payload.get("conversation_handoff") if isinstance(payload, dict) else None
    claim_key = handoff.get("claim_key") if isinstance(handoff, dict) else None
    if not isinstance(claim_key, str) or not claim_key.strip():
        claim_key = None
    return ContextPackEntry(
        category=category,
        content=row["content"],
        citation_label=source_scope.ref or source_scope.scope_type,
        memory_type=row["memory_type"],
        subtype=row["subtype"],
        importance=row["importance"],
        sensitivity=SensitivityLevel(row["sensitivity"]),
        visibility=row["visibility"],
        assertion_source=assertion_source,
        verification_state=verification_state,
        source_scope=source_scope,
        evidence_status=str(row["evidence_status"]),
        unverified_user_report=is_unverified_user_report(assertion_source, verification_state),
        version=int(row["version"]),
        claim_key=claim_key,
    )


def _truncate_pack_entry(entry: ContextPackEntry, remaining_tokens: int) -> ContextPackEntry | None:
    if remaining_tokens < 40:
        return None
    content = entry.content
    while content:
        candidate = entry.model_copy(update={"content": content + "…"})
        if _estimate_context_tokens(candidate.model_dump_json()) <= remaining_tokens:
            return candidate
        new_length = max(0, len(content) - max(1, len(content) // 8))
        content = content[:new_length].rstrip()
    return None


def build_context_pack(
    profile: ConnectionProfile | DatabaseProfile | str | Path,
    *,
    query: str,
    project_id: str | None = None,
    token_budget: int = 3500,
    max_sensitivity: str | None = None,
    top_k: int = 20,
    search_query: str | None = None,
    read_index: Any | None = None,
    embedding: Any | None = None,
) -> ContextPackResult:
    """Build a bounded, provenance-aware context pack without generating an answer."""

    if isinstance(token_budget, bool) or not 1000 <= token_budget <= 8000:
        raise SkillApiBlocked("token_budget must be between 1000 and 8000")
    if not 1 <= top_k <= 50:
        raise SkillApiBlocked("top_k must be between 1 and 50")
    profile_value = _profile(profile)
    context = query_context(
        profile_value,
        query=query,
        search_query=search_query,
        project_id=project_id,
        top_k=top_k,
        max_sensitivity=max_sensitivity,
        max_content_chars=min(2000, max(200, token_budget)),
        read_index=read_index,
        embedding=embedding,
    )
    project = str(context.scope["project_id"])
    sensitivity = SensitivityLevel(str(context.scope["max_sensitivity"]))
    canonical = profile_value.canonical_db.resolve()
    statuses = ("active", "pending_review", "superseded", "archived", "rejected")
    scope_clause, scope_params = _context_scope(profile_value, project, sensitivity, statuses)
    current_rows: list[sqlite3.Row] = []
    history_rows: list[sqlite3.Row] = []
    conflict_rows: list[sqlite3.Row] = []
    current_ids = tuple(hit.memory_id for hit in context.hits)
    with _readonly_connection(canonical) as connection:
        for memory_id in current_ids:
            row = _context_row(connection, scope_clause, scope_params, memory_id=memory_id)
            if row is not None and row["status"] == "active":
                current_rows.append(row)
        seen_history: set[str] = set()
        for current in current_rows:
            parent_id = current["supersedes_id"]
            depth = 0
            while parent_id and depth < 5 and parent_id not in seen_history:
                parent = _context_row(connection, scope_clause, scope_params, memory_id=parent_id)
                if parent is None:
                    break
                seen_history.add(parent_id)
                history_rows.append(parent)
                parent_id = parent["supersedes_id"]
                depth += 1
        seen_conflicts: set[str] = set()
        for current in current_rows:
            payload = json.loads(current["domain_payload_json"])
            handoff = payload.get("conversation_handoff") if isinstance(payload, dict) else None
            claim_key = handoff.get("claim_key") if isinstance(handoff, dict) else None
            if not isinstance(claim_key, str) or not claim_key.strip():
                continue
            conflict_clause, conflict_params = _context_scope(
                profile_value,
                project,
                sensitivity,
                ("active", "pending_review"),
            )
            for conflict in connection.execute(
                f"""SELECT m.memory_id,m.version,m.content,m.memory_type,m.subtype,m.importance,
                           m.sensitivity,m.visibility,m.assertion_source,m.verification_state,
                           m.source_scope_json,m.domain_payload_json,m.supersedes_id,m.status,
                           CASE WHEN EXISTS (
                             SELECT 1 FROM memory_evidence e
                             WHERE e.memory_id=m.memory_id AND e.evidence_status='verified'
                           ) THEN 'verified' ELSE 'unverified' END AS evidence_status
                    FROM memory_items m
                    WHERE json_extract(m.domain_payload_json,'$.conversation_handoff.claim_key')=?
                      AND {conflict_clause}
                    ORDER BY CASE m.status WHEN 'pending_review' THEN 0 ELSE 1 END,m.updated_at DESC""",
                [claim_key, *conflict_params],
            ).fetchall():
                if conflict["memory_id"] in current_ids or conflict["memory_id"] in seen_conflicts:
                    continue
                seen_conflicts.add(conflict["memory_id"])
                conflict_rows.append(conflict)

    current_entries = [_pack_entry_from_row(row, "current") for row in current_rows]
    conflict_entries = [_pack_entry_from_row(row, "conflict") for row in conflict_rows]
    history_entries = [_pack_entry_from_row(row, "history") for row in history_rows]
    current_entries.sort(key=lambda entry: {"critical": 0, "high": 1, "medium": 2, "low": 3}.get(entry.importance, 4))

    base_material = json.dumps(
        {
            "query": context.query,
            "scope": context.scope,
            "token_budget": token_budget,
            "contract": "skill-context-pack-v1",
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    estimated = _estimate_context_tokens(base_material) + 200
    remaining = token_budget - estimated
    selected: dict[str, list[ContextPackEntry]] = {"current": [], "conflict": [], "history": []}
    omitted_counts = {"current": 0, "conflict": 0, "history": 0}
    truncated = False
    for category, entries in (
        ("current", current_entries),
        ("conflict", conflict_entries),
        ("history", history_entries),
    ):
        for entry in entries:
            entry_tokens = _estimate_context_tokens(entry.model_dump_json())
            if entry_tokens <= remaining:
                selected[category].append(entry)
                remaining -= entry_tokens
                continue
            compact = _truncate_pack_entry(entry, remaining)
            if compact is not None:
                compact_tokens = _estimate_context_tokens(compact.model_dump_json())
                selected[category].append(compact)
                remaining -= compact_tokens
                truncated = True
                continue
            if category == "current" and entry.importance == "critical":
                raise SkillApiBlocked("critical context entry does not fit within token_budget")
            omitted_counts[category] += 1
            truncated = True

    while True:
        source_map: dict[str, ContextSourceRef] = {}
        for entry in [*selected["current"], *selected["conflict"], *selected["history"]]:
            existing = source_map.get(entry.citation_label)
            source_ref = ContextSourceRef(
                citation_label=entry.citation_label,
                verification_state=entry.verification_state,
                unverified_user_report=entry.unverified_user_report,
            )
            if existing is None or (existing.unverified_user_report is False and source_ref.unverified_user_report):
                source_map[entry.citation_label] = source_ref
        source_refs = list(source_map.values())
        source_tokens = sum(_estimate_context_tokens(item.model_dump_json()) for item in source_refs)
        entry_tokens = sum(
            _estimate_context_tokens(entry.model_dump_json())
            for entry in [*selected["current"], *selected["conflict"], *selected["history"]]
        )
        final_estimate = estimated + source_tokens + entry_tokens
        if final_estimate <= token_budget:
            estimated = final_estimate
            break
        if selected["history"]:
            selected["history"].pop()
            omitted_counts["history"] += 1
            truncated = True
            continue
        adjusted = False
        for category in ("current", "conflict"):
            if not selected[category]:
                continue
            original = selected[category].pop()
            overflow = final_estimate - token_budget
            target_tokens = max(
                40,
                _estimate_context_tokens(original.model_dump_json()) - overflow - 20,
            )
            compact = _truncate_pack_entry(original, target_tokens)
            if compact is not None:
                selected[category].append(compact)
                truncated = True
                adjusted = True
                break
            if category == "current" and original.importance == "critical":
                raise SkillApiBlocked("critical context entry does not fit within token_budget")
            omitted_counts[category] += 1
            truncated = True
            adjusted = True
            break
        if adjusted:
            continue
        raise SkillApiBlocked("context pack metadata exceeds token_budget")
    warnings = list(context.warnings)
    if truncated:
        warnings.append("Context Pack token budget omitted or truncated lower-priority entries.")
    if any(entry.unverified_user_report for entry in [*selected["current"], *selected["conflict"]]):
        warnings.append("部分上下文来自未外部验证的用户陈述。")
    return ContextPackResult(
        backend=context.backend,
        query=context.query,
        token_budget=token_budget,
        estimated_tokens=estimated,
        truncated=truncated,
        evidence_completeness=context.evidence_completeness,
        current=selected["current"],
        conflicts=selected["conflict"],
        history=selected["history"],
        source_refs=source_refs,
        omitted_counts=omitted_counts,
        warnings=warnings,
        scope=context.scope,
    )


def query_ask(
    profile: ConnectionProfile | DatabaseProfile | str | Path,
    *,
    query: str,
    project_id: str | None = None,
    top_k: int = 5,
    max_sensitivity: str | None = None,
    read_index: Any | None = None,
    embedding: Any | None = None,
) -> QueryAnswerResult:
    context = query_context(
        profile,
        query=query,
        project_id=project_id,
        top_k=top_k,
        max_sensitivity=max_sensitivity,
        read_index=read_index,
        embedding=embedding,
    )
    if not context.hits:
        return QueryAnswerResult(
            backend=context.backend,
            query=context.query,
            answer="未检索到可用记忆。",
            uncertainties=["没有检索到可引用证据。"],
            evidence_completeness="no_evidence",
            scope=context.scope,
        )
    points = [hit.content for hit in context.hits]
    uncertainties = list(context.warnings)
    if any(hit.unverified_user_report for hit in context.hits):
        uncertainties.append("部分内容来自未外部验证的用户陈述。")
    uncertainties.append("当前使用 SQLite FTS 摘录，未执行语义完整度评估。")
    source_ranges = list(dict.fromkeys(hit.citation_label for hit in context.hits))
    return QueryAnswerResult(
        backend=context.backend,
        query=context.query,
        answer="根据当前数据库中的可检索证据：" + "；".join(points),
        points=points,
        source_ranges=source_ranges,
        uncertainties=uncertainties,
        evidence_completeness=context.evidence_completeness,
        scope=context.scope,
    )

def get_memory(
    profile: ConnectionProfile | DatabaseProfile | str | Path,
    *,
    memory_id: str,
    project_id: str | None = None,
    max_sensitivity: str | None = None,
    max_content_chars: int = 4000,
    include_evidence_quote: bool = False,
) -> MemoryDetailResult:
    """Return one scoped canonical memory without synthesizing an answer."""

    profile_value = _profile(profile)
    if "query" not in profile_value.capabilities:
        raise SkillApiBlocked("profile capabilities do not allow query")
    target = memory_id.strip()
    if not target:
        raise SkillApiBlocked("memory_id must not be empty")
    if not 100 <= max_content_chars <= 4000:
        raise SkillApiBlocked("max_content_chars must be between 100 and 4000")
    project = _project(profile_value, project_id)
    sensitivity = _max_sensitivity(profile_value, max_sensitivity)
    canonical = profile_value.canonical_db.resolve()
    if not canonical.is_file():
        raise SkillApiBlocked("canonical database does not exist")
    scope_clause, scope_params = _scope(profile_value, project, sensitivity)

    with _readonly_connection(canonical) as connection:
        tables = {
            row["name"] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
            )
        }
        if "memory_items" not in tables or "memory_evidence" not in tables:
            raise SkillApiBlocked("canonical database does not expose memory_items and memory_evidence")
        row = connection.execute(
            f"""SELECT m.memory_id,m.version,m.content,m.memory_type,m.subtype,m.subject,
                       m.importance,m.sensitivity,m.visibility,m.assertion_source,
                       m.verification_state,m.source_scope_json
                FROM memory_items m
                WHERE m.memory_id=? AND {scope_clause}""",
            [target, *scope_params],
        ).fetchone()
        if row is None:
            raise SkillApiBlocked("memory was not found in the requested scope")
        evidence_rows = connection.execute(
            """SELECT evidence_status,locator_json,quote
               FROM memory_evidence WHERE memory_id=? ORDER BY evidence_id""",
            (target,),
        ).fetchall()

    source_scope = SourceScope.model_validate(json.loads(row["source_scope_json"]))
    assertion_source = AssertionSource(row["assertion_source"])
    verification_state = VerificationState(row["verification_state"])
    unverified = is_unverified_user_report(assertion_source, verification_state)
    citation_label = source_scope.ref or source_scope.scope_type
    evidences = [
        MemoryEvidenceView(
            citation_label=citation_label,
            status=evidence["evidence_status"],
            locator_json=json.loads(evidence["locator_json"]),
            quote=(evidence["quote"][:max_content_chars] if include_evidence_quote else None),
        )
        for evidence in evidence_rows
    ]
    warnings = ["部分内容来自未外部验证的用户陈述。"] if unverified else []
    return MemoryDetailResult(
        backend=profile_value.backend,
        memory_id=row["memory_id"],
        version=int(row["version"]),
        content=row["content"][:max_content_chars],
        memory_type=row["memory_type"],
        subtype=row["subtype"],
        subject=row["subject"],
        importance=row["importance"],
        sensitivity=SensitivityLevel(row["sensitivity"]),
        visibility=row["visibility"],
        assertion_source=assertion_source,
        verification_state=verification_state,
        source_scope=source_scope,
        unverified_user_report=unverified,
        evidences=evidences,
        warnings=warnings,
        scope={
            "workspace_id": profile_value.workspace_id,
            "project_id": project,
            "domain_profile": profile_value.domain_profile,
            "max_sensitivity": sensitivity.value,
        },
    )


def health_check(
    profile: ConnectionProfile | DatabaseProfile | str | Path,
    *,
    project_id: str | None = None,
    read_index: Any | None = None,
) -> HealthResult:
    profile_value = _profile(profile)
    if "health" not in profile_value.capabilities:
        raise SkillApiBlocked("profile capabilities do not allow health")
    project = _project(profile_value, project_id)
    if profile_value.backend == SkillBackend.MILVUS and read_index is None:
        return HealthResult(
            status="blocked",
            backend=SkillBackend.MILVUS,
            consistency_status="not_assessed",
            quality_status="not_assessed",
            freshness_status="unknown",
            checks={"read_index_available": False},
            warnings=["milvus health requires a read-only index adapter"],
            scope={"workspace_id": profile_value.workspace_id, "project_id": project, "domain_profile": profile_value.domain_profile},
        )
    canonical = profile_value.canonical_db.resolve()
    if not canonical.is_file():
        return HealthResult(
            status="blocked",
            consistency_status="not_assessed",
            quality_status="critical",
            freshness_status="unknown",
            checks={"canonical_exists": False},
            warnings=["canonical database does not exist"],
            scope={"workspace_id": profile_value.workspace_id, "project_id": project, "domain_profile": profile_value.domain_profile},
        )

    checks: dict[str, bool] = {"canonical_exists": True}
    warnings: list[str] = []
    counts: dict[str, int] = {}
    with _readonly_connection(canonical) as connection:
        tables = {row["name"] for row in connection.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
        checks["schema_migrations"] = "schema_migrations" in tables
        checks["memory_items"] = "memory_items" in tables
        checks["fts_table"] = "memory_fts" in tables
        checks["memory_evidence"] = "memory_evidence" in tables
        checks["memory_outbox"] = "memory_outbox" in tables
        checks["fts_sync_state"] = "memory_fts_sync_state" in tables
        checks["integrity"] = connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        checks["foreign_keys"] = len(connection.execute("PRAGMA foreign_key_check").fetchall()) == 0
        applied = {row["migration_id"] for row in connection.execute("SELECT migration_id FROM schema_migrations")} if "schema_migrations" in tables else set()
        checks["migrations_complete"] = {p.stem for p in MIGRATIONS_DIR.glob("*.sql")}.issubset(applied)
        required_tables = ("memory_items", "fts_table", "memory_evidence", "memory_outbox", "fts_sync_state")
        if not all(checks.get(name, False) for name in required_tables):
            return HealthResult(
                status="critical",
                consistency_status="critical",
                quality_status="critical",
                freshness_status="unknown",
                checks=checks,
                counts=counts,
                warnings=["required canonical tables are missing"],
                scope={"workspace_id": profile_value.workspace_id, "project_id": project, "domain_profile": profile_value.domain_profile},
            )
        scope_clause, scope_params = _scope(profile_value, project, profile_value.max_sensitivity)
        checks["scope_exists"] = connection.execute(
            "SELECT 1 FROM workspaces WHERE workspace_id=?", (profile_value.workspace_id,)
        ).fetchone() is not None and connection.execute(
            "SELECT 1 FROM projects WHERE project_id=? AND workspace_id=?",
            (project, profile_value.workspace_id),
        ).fetchone() is not None
        counts["active"] = int(connection.execute(
            f"SELECT COUNT(*) FROM memory_items m WHERE {scope_clause}", scope_params
        ).fetchone()[0])
        counts["missing_verified_evidence"] = int(connection.execute(
            f"""SELECT COUNT(*) FROM memory_items m WHERE {scope_clause}
                AND NOT EXISTS (SELECT 1 FROM memory_evidence e
                                WHERE e.memory_id=m.memory_id AND e.evidence_status='verified')""",
            scope_params,
        ).fetchone()[0])
        checks["verified_evidence"] = counts["missing_verified_evidence"] == 0
        invalid_assertions = 0
        try:
            domain_profile = ProfileRegistry().get(profile_value.domain_profile, "1.0.0")
        except KeyError:
            domain_profile = None
            warnings.append("domain profile is not registered; generic assertion policy is used")
        for row in connection.execute(
            f"""SELECT m.assertion_source,m.verification_state,m.memory_type,m.status
                FROM memory_items m WHERE {scope_clause}""",
            scope_params,
        ):
            try:
                if domain_profile is None:
                    validate_assertion_policy(
                        assertion_source=AssertionSource(row["assertion_source"]),
                        verification_state=VerificationState(row["verification_state"]),
                        status=MemoryStatus(row["status"]),
                    )
                else:
                    domain_profile.validate_assertion_state(
                        assertion_source=AssertionSource(row["assertion_source"]),
                        verification_state=VerificationState(row["verification_state"]),
                        memory_type=row["memory_type"],
                        status=MemoryStatus(row["status"]),
                    )
            except (ValueError, KeyError):
                invalid_assertions += 1
        counts["invalid_assertion_states"] = invalid_assertions
        checks["assertion_states"] = invalid_assertions == 0

        fts_mismatches = 0
        if checks["fts_table"] and checks["memory_items"]:
            expected = list(connection.execute(
                f"SELECT m.memory_id,m.version,m.content FROM memory_items m WHERE {scope_clause}",
                scope_params,
            ))
            for row in expected:
                fts = connection.execute(
                    "SELECT content FROM memory_fts WHERE memory_id=?", (row["memory_id"],)
                ).fetchone()
                sync = connection.execute(
                    "SELECT item_version,deleted FROM memory_fts_sync_state WHERE memory_id=?",
                    (row["memory_id"],),
                ).fetchone()
                if fts is None or sha256_text(fts["content"]) != sha256_text(row["content"]):
                    fts_mismatches += 1
                elif sync is None or int(sync["item_version"]) != int(row["version"]) or int(sync["deleted"]) != 0:
                    fts_mismatches += 1
            ghost = connection.execute(
                """SELECT COUNT(*) FROM memory_fts f
                   LEFT JOIN memory_items m ON m.memory_id=f.memory_id
                   WHERE f.workspace_id=? AND f.project_id=? AND f.domain_profile=?
                     AND (m.memory_id IS NULL OR m.status!='active' OR m.deleted_at IS NOT NULL)""",
                (profile_value.workspace_id, project, profile_value.domain_profile),
            ).fetchone()[0]
            fts_mismatches += int(ghost)
        counts["fts_mismatches"] = fts_mismatches
        checks["fts_consistency"] = fts_mismatches == 0
        outbox = {
            row["status"]: int(row["count"]) for row in connection.execute(
                """SELECT status,COUNT(*) AS count FROM memory_outbox
                   WHERE workspace_id=? GROUP BY status""",
                (profile_value.workspace_id,),
            )
        }
    counts["outbox_pending"] = int(outbox.get("pending", 0) + outbox.get("claimed", 0))
    counts["outbox_poison"] = int(outbox.get("poison", 0))
    consistency = "ok" if checks.get("fts_consistency") else "critical"
    if profile_value.backend == SkillBackend.MILVUS:
        milvus_checks: list[bool] = []
        try:
            aliases = read_index.aliases()
            alias_target = aliases.get(profile_value.read_alias) if profile_value.read_alias else None
            checks["milvus_alias_target_matches"] = alias_target == profile_value.physical_collection
            milvus_checks.append(checks["milvus_alias_target_matches"])
            collection = read_index.describe_collection()
            checks["milvus_collection_metadata"] = bool(collection)
            milvus_checks.append(checks["milvus_collection_metadata"])
            iterator = read_index.iter_rows(
                filter="",
                batch_size=1,
                output_fields=["memory_id", "embedding_model", "embedding_dim", "index_schema_version"],
            )
            try:
                sample = next(iter(iterator))
            finally:
                close = getattr(iterator, "close", None)
                if close is not None:
                    close()
            checks["milvus_embedding_model_matches"] = sample.get("embedding_model") == profile_value.embedding_model
            checks["milvus_embedding_dimension_matches"] = int(sample.get("embedding_dim") or 0) == int(profile_value.embedding_dimension or 0)
            checks["milvus_index_schema_matches"] = sample.get("index_schema_version") == profile_value.index_schema_version
            milvus_checks.extend([
                checks["milvus_embedding_model_matches"],
                checks["milvus_embedding_dimension_matches"],
                checks["milvus_index_schema_matches"],
            ])
            milvus_count = int(read_index.count(filter=_milvus_filter(profile_value, project, profile_value.max_sensitivity)))
            counts["milvus_count"] = milvus_count
            checks["milvus_count_matches"] = milvus_count == counts["active"]
            milvus_checks.append(checks["milvus_count_matches"])
        except Exception as exc:
            checks["milvus_readonly_checks_complete"] = False
            warnings.append(f"milvus health check failed: {type(exc).__name__}")
            milvus_checks.append(False)
        else:
            checks["milvus_readonly_checks_complete"] = True
        if not all(milvus_checks):
            consistency = "critical"
        else:
            consistency = "not_assessed"
            warnings.append("milvus count and metadata checks passed; full 5-tuple reconcile was not performed")
    quality_failed = not all(checks.get(name, False) for name in (
        "integrity", "foreign_keys", "migrations_complete", "scope_exists",
        "verified_evidence", "assertion_states",
    ))
    quality_warnings = [value for value in warnings if not value.startswith("milvus")]
    quality = "critical" if quality_failed else ("warning" if quality_warnings else "ok")
    if counts["outbox_poison"]:
        freshness = "stale"
        warnings.append("outbox contains poison events")
    elif counts["outbox_pending"]:
        freshness = "stale"
        warnings.append("outbox has pending or claimed events")
    elif not checks.get("fts_table"):
        freshness = "unknown"
    else:
        freshness = "current"
    if consistency == "critical" or quality == "critical":
        status = "critical"
    elif consistency == "not_assessed" or freshness in {"stale", "unknown"} or quality == "warning":
        status = "warning"
    else:
        status = "ok"
    return HealthResult(
        status=status,
        consistency_status=consistency,
        quality_status=quality,
        freshness_status=freshness,
        checks=checks,
        counts=counts,
        warnings=warnings,
        scope={"workspace_id": profile_value.workspace_id, "project_id": project, "domain_profile": profile_value.domain_profile},
    )