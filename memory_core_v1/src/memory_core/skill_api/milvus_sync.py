from __future__ import annotations

import hashlib
import os
import re
import sqlite3
from pathlib import Path
from collections.abc import Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ..config import EmbeddingSettings
from ..db import MIGRATIONS_DIR, SQLiteStore
from ..embeddings import OpenAICompatibleEmbeddingClient
from ..fakes import FakeEmbedding, FakeVectorIndex
from ..outbox import OutboxWorker
from ..profiles import ProfileRegistry
from ..pymilvus_index import PymilvusMemoryIndex
from .attach import load_database_profile
from .models import DatabaseProfile, SkillApiBlocked, SkillBackend

SAFE_MILVUS_NAME = re.compile(r"^(?!.*prod)[A-Za-z0-9_.-]+$", re.IGNORECASE)


class _CountingEmbedding:
    def __init__(self, delegate: Any) -> None:
        self.delegate = delegate
        self.calls = 0
        self.model = str(getattr(delegate, "model", ""))
        self.dimension = int(getattr(delegate, "dimension", 0))

    def embed_one(self, text: str) -> list[float]:
        self.calls += 1
        return self.delegate.embed_one(text)

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return self.delegate.embed(texts)


class MilvusSyncResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_version: Literal["skill-milvus-sync-v1"] = "skill-milvus-sync-v1"
    status: Literal["completed", "partial", "nothing_to_do"] = "nothing_to_do"
    mode: Literal["fake", "real"] = "fake"
    workspace_id: str
    physical_collection: str
    read_alias: str
    write_alias: str
    embedding_model: str
    embedding_dimension: int
    index_schema_version: str
    processed: dict[str, int] = Field(default_factory=dict)
    outbox_before: dict[str, int] = Field(default_factory=dict)
    outbox_after: dict[str, int] = Field(default_factory=dict)
    embedding_calls: int = 0
    checks: dict[str, bool] = Field(default_factory=dict)
    warnings: tuple[str, ...] = ()


def _safe_name(value: str | None, *, label: str) -> str:
    name = (value or "").strip()
    if not name or not SAFE_MILVUS_NAME.fullmatch(name) or "prod" in name.casefold():
        raise SkillApiBlocked(f"{label} must be a safe non-production Milvus name")
    return name


def _outbox_counts(store: SQLiteStore, workspace_id: str) -> dict[str, int]:
    with store.read() as connection:
        return {
            str(row["status"]): int(row["count"])
            for row in connection.execute(
                """SELECT status,COUNT(*) AS count FROM memory_outbox
                   WHERE workspace_id=? GROUP BY status ORDER BY status""",
                (workspace_id,),
            )
        }


def _pending_count(counts: dict[str, int]) -> int:
    return int(counts.get("pending", 0) + counts.get("claimed", 0))


def _validated_sync_store(profile: DatabaseProfile) -> SQLiteStore:
    canonical = profile.canonical_db.resolve()
    if not canonical.is_file():
        raise SkillApiBlocked("canonical database does not exist")
    store = SQLiteStore(canonical)
    try:
        connection = store.connect(read_only=True)
    except sqlite3.DatabaseError as exc:
        raise SkillApiBlocked("canonical database is not readable") from exc
    try:
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise SkillApiBlocked("canonical database integrity check failed")
        tables = {
            row["name"]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        required = {"schema_migrations", "memory_items", "memory_outbox", "memory_fts"}
        if not required.issubset(tables):
            raise SkillApiBlocked("canonical database is missing required sync tables")
        expected_migrations = {
            path.stem: hashlib.sha256(path.read_text(encoding="utf-8").encode("utf-8")).hexdigest()
            for path in sorted(MIGRATIONS_DIR.glob("*.sql"))
        }
        applied = {
            row["migration_id"]: row["checksum"]
            for row in connection.execute("SELECT migration_id,checksum FROM schema_migrations")
        }
        if applied != expected_migrations:
            raise SkillApiBlocked("canonical database migration state does not match the current schema")
        workspace = connection.execute(
            "SELECT 1 FROM workspaces WHERE workspace_id=?", (profile.workspace_id,)
        ).fetchone()
        if workspace is None:
            raise SkillApiBlocked("workspace_id is not present in canonical_db")
        project_placeholders = ",".join("?" for _ in profile.project_ids)
        project_count = connection.execute(
            f"SELECT COUNT(*) FROM projects WHERE workspace_id=? AND project_id IN ({project_placeholders})",
            (profile.workspace_id, *profile.project_ids),
        ).fetchone()[0]
        if int(project_count) != len(set(profile.project_ids)):
            raise SkillApiBlocked("one or more profile projects are missing from canonical_db")
        try:
            domain = ProfileRegistry().get(profile.domain_profile, profile.profile_version)
        except KeyError as exc:
            raise SkillApiBlocked("domain profile is not registered") from exc
        registered = connection.execute(
            "SELECT domain FROM domain_profiles WHERE profile_id=? AND version=?",
            (profile.domain_profile, profile.profile_version),
        ).fetchone()
        if registered is None or registered["domain"] != domain.domain:
            raise SkillApiBlocked("domain profile is not registered in canonical_db")
    except sqlite3.DatabaseError as exc:
        raise SkillApiBlocked("canonical database schema validation failed") from exc
    finally:
        connection.close()
    return store


def _real_index(profile: DatabaseProfile, *, uri_env: str) -> PymilvusMemoryIndex:
    uri = os.environ.get(uri_env)
    if not uri:
        raise SkillApiBlocked(f"environment variable is not configured: {uri_env}")
    try:
        index = PymilvusMemoryIndex(
            uri=uri,
            collection_name=profile.physical_collection,
            read_alias=profile.read_alias or "",
            write_alias=profile.write_alias or "",
            dimension=int(profile.embedding_dimension or 0),
            metric_type=profile.metric_type or "COSINE",
        )
        aliases = index.aliases()
    except Exception as exc:
        raise SkillApiBlocked(f"Milvus sync adapter initialization failed: {type(exc).__name__}") from exc
    if not index.client.has_collection(profile.physical_collection):
        raise SkillApiBlocked("physical collection does not exist")
    if aliases.get(profile.read_alias) != profile.physical_collection:
        raise SkillApiBlocked("read alias does not point to the configured physical collection")
    if aliases.get(profile.write_alias) != profile.physical_collection:
        raise SkillApiBlocked("write alias does not point to the configured physical collection")
    return index


def _real_embedding(
    profile: DatabaseProfile,
    *,
    key_env: str,
    base_url_env: str,
    model_env: str,
    dimension_env: str,
) -> OpenAICompatibleEmbeddingClient:
    api_key = os.environ.get(key_env)
    if not api_key:
        raise SkillApiBlocked(f"environment variable is not configured: {key_env}")
    model = os.environ.get(model_env) or profile.embedding_model or ""
    dimension_text = os.environ.get(dimension_env) or str(profile.embedding_dimension or "")
    try:
        dimension = int(dimension_text)
    except ValueError as exc:
        raise SkillApiBlocked("configured embedding dimension is invalid") from exc
    if model != profile.embedding_model or dimension != profile.embedding_dimension:
        raise SkillApiBlocked("configured embedding metadata does not match the Milvus profile")
    settings = EmbeddingSettings(
        base_url=os.environ.get(base_url_env, "https://api.siliconflow.cn/v1"),
        api_key=api_key,
        model=model,
        dimension=dimension,
    )
    return OpenAICompatibleEmbeddingClient(settings)


def sync_milvus_outbox(
    profile: DatabaseProfile | str | Path,
    *,
    fake: bool = False,
    vector_index: Any | None = None,
    embedding: Any | None = None,
    owner: str = "database-skill-sync",
    limit: int = 100,
    lease_seconds: int = 60,
    uri_env: str = "MILVUS_URI",
    embedding_key_env: str = "EMBEDDING_API_KEY",
    embedding_base_url_env: str = "EMBEDDING_BASE_URL",
    embedding_model_env: str = "EMBEDDING_MODEL",
    embedding_dim_env: str = "EMBEDDING_DIM",
) -> MilvusSyncResult:
    """Process canonical Outbox events into FTS and the configured Milvus alias."""

    if isinstance(profile, DatabaseProfile):
        profile_value = profile
    else:
        profile_value = load_database_profile(profile)
    if profile_value.backend != SkillBackend.MILVUS:
        raise SkillApiBlocked("milvus sync requires a milvus database profile")
    if "write" not in profile_value.capabilities:
        raise SkillApiBlocked("profile capabilities do not allow index sync")
    physical_collection = _safe_name(profile_value.physical_collection, label="physical_collection")
    read_alias = _safe_name(profile_value.read_alias, label="read_alias")
    write_alias = _safe_name(profile_value.write_alias, label="write_alias")
    if not profile_value.embedding_model or not profile_value.embedding_dimension:
        raise SkillApiBlocked("profile must contain embedding model and dimension")
    if not profile_value.project_ids:
        raise SkillApiBlocked("profile must contain at least one project")
    if isinstance(limit, bool) or not 1 <= limit <= 1000:
        raise SkillApiBlocked("limit must be between 1 and 1000")
    if isinstance(lease_seconds, bool) or not 1 <= lease_seconds <= 3600:
        raise SkillApiBlocked("lease_seconds must be between 1 and 3600")
    if not owner.strip():
        raise SkillApiBlocked("owner must be non-empty")

    store = _validated_sync_store(profile_value)
    checks = {"canonical_valid": True}
    warnings: list[str] = []
    mode: Literal["fake", "real"] = "fake" if fake else "real"
    if vector_index is None:
        if fake:
            vector_index = FakeVectorIndex(read_alias=read_alias, write_alias=write_alias)
        else:
            vector_index = _real_index(profile_value, uri_env=uri_env)
            checks["milvus_aliases_match"] = True
    if embedding is None:
        if fake:
            embedding = FakeEmbedding(
                dimension=int(profile_value.embedding_dimension),
                model=profile_value.embedding_model,
            )
        else:
            embedding = _real_embedding(
                profile_value,
                key_env=embedding_key_env,
                base_url_env=embedding_base_url_env,
                model_env=embedding_model_env,
                dimension_env=embedding_dim_env,
            )
            checks["embedding_configured"] = True
    if (
        str(getattr(embedding, "model", profile_value.embedding_model)) != profile_value.embedding_model
        or int(getattr(embedding, "dimension", profile_value.embedding_dimension)) != int(profile_value.embedding_dimension)
    ):
        raise SkillApiBlocked("embedding adapter metadata does not match the Milvus profile")

    before = _outbox_counts(store, profile_value.workspace_id)
    counted_embedding = _CountingEmbedding(embedding)
    worker = OutboxWorker(
        store=store,
        profiles=ProfileRegistry(),
        embedding=counted_embedding,
        vector_index=vector_index,
        write_alias=write_alias,
        index_schema_version=profile_value.index_schema_version or "memory-index-v1",
        embedding_model_override=profile_value.embedding_model,
        embedding_dim_override=int(profile_value.embedding_dimension),
        index_schema_version_override=profile_value.index_schema_version or "memory-index-v1",
    )
    try:
        processed = worker.run_once(owner=owner.strip(), limit=limit, lease_seconds=lease_seconds)
    except Exception as exc:
        raise SkillApiBlocked(f"milvus sync worker failed: {type(exc).__name__}") from exc
    after = _outbox_counts(store, profile_value.workspace_id)
    embedding_calls = counted_embedding.calls
    failures = int(processed.get("poison", 0) + processed.get("pending", 0))
    after_poison = int(after.get("poison", 0))
    if _pending_count(after) or after_poison or failures:
        status: Literal["completed", "partial", "nothing_to_do"] = "partial"
        if failures or after_poison:
            warnings.append("one or more Outbox events require retry or manual poison recovery")
    elif processed:
        status = "completed"
    else:
        status = "nothing_to_do"
    return MilvusSyncResult(
        status=status,
        mode=mode,
        workspace_id=profile_value.workspace_id,
        physical_collection=physical_collection,
        read_alias=read_alias,
        write_alias=write_alias,
        embedding_model=profile_value.embedding_model,
        embedding_dimension=int(profile_value.embedding_dimension),
        index_schema_version=profile_value.index_schema_version or "memory-index-v1",
        processed={str(key): int(value) for key, value in sorted(processed.items())},
        outbox_before=before,
        outbox_after=after,
        embedding_calls=embedding_calls,
        checks=checks,
        warnings=tuple(warnings),
    )
