from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence

from .models import (
    AccessGrant,
    AccessScope,
    EvidenceStatus,
    MemoryEvidence,
    MemoryRecord,
    MemoryStatus,
    SENSITIVITY_ORDER,
    SensitivityLevel,
    SourceScope,
)
from .profiles import ProfileRegistry


MIGRATIONS_DIR = Path(os.environ.get("MEMORY_CORE_MIGRATIONS_DIR", str(Path(__file__).resolve().parents[2] / "migrations"))).resolve()


def utc_now_ts() -> int:
    return int(time.time())


def dt_to_ts(value: datetime | None) -> int | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return int(value.astimezone(timezone.utc).timestamp())


def ts_to_dt(value: int | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromtimestamp(int(value), tz=timezone.utc)


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def json_loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    return json.loads(value)


class SQLiteStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def connect(self, *, read_only: bool = False) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if read_only:
            connection = sqlite3.connect(f"file:{self.path.as_posix()}?mode=ro", uri=True)
        else:
            connection = sqlite3.connect(self.path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        if not read_only:
            connection.execute("PRAGMA journal_mode=WAL")
        return connection

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect(read_only=False)
        try:
            yield connection
        finally:
            connection.close()

    def migrate(self) -> None:
        migration_paths = sorted(MIGRATIONS_DIR.glob("*.sql"))
        if not migration_paths:
            raise RuntimeError(f"no migration files found in {MIGRATIONS_DIR}")
        with self.transaction() as connection:
            self._rename_legacy_tables(connection)
            migration_table_exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
            ).fetchone() is not None
            for migration_path in migration_paths:
                migration_id = migration_path.stem
                script = migration_path.read_text(encoding="utf-8")
                checksum = hashlib.sha256(script.encode("utf-8")).hexdigest()
                if not migration_table_exists:
                    if migration_path.name != "001_core.sql":
                        raise RuntimeError(
                            f"initial migration must create schema_migrations before {migration_id}"
                        )
                    connection.executescript(script)
                    connection.execute(
                        "INSERT INTO schema_migrations(migration_id,checksum,applied_at) VALUES(?,?,?)",
                        (migration_id, checksum, utc_now_ts()),
                    )
                    migration_table_exists = True
                    continue
                row = connection.execute(
                    "SELECT checksum FROM schema_migrations WHERE migration_id=?",
                    (migration_id,),
                ).fetchone()
                if row:
                    if row["checksum"] != checksum:
                        raise RuntimeError(f"{migration_id} migration checksum mismatch")
                    continue
                connection.executescript(script)
                connection.execute(
                    "INSERT INTO schema_migrations(migration_id,checksum,applied_at) VALUES(?,?,?)",
                    (migration_id, checksum, utc_now_ts()),
                )

    @staticmethod
    def _rename_legacy_tables(connection: sqlite3.Connection) -> None:
        tables = {
            row["name"] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "schema_migrations" in tables:
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(schema_migrations)")}
            if "migration_id" not in columns and "etl_schema_migrations" not in tables:
                connection.execute("ALTER TABLE schema_migrations RENAME TO etl_schema_migrations")
                tables.remove("schema_migrations")
                tables.add("etl_schema_migrations")
        if "sources" in tables:
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(sources)")}
            if "workspace_id" not in columns and "etl_sources_v1" not in tables:
                connection.execute("ALTER TABLE sources RENAME TO etl_sources_v1")

    def ensure_workspace_project(self, *, workspace_id: str, workspace_name: str, project_id: str, project_name: str) -> None:
        now = utc_now_ts()
        with self.transaction() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO workspaces(workspace_id,name,created_at) VALUES(?,?,?)",
                (workspace_id, workspace_name, now),
            )
            connection.execute(
                "INSERT OR IGNORE INTO projects(project_id,workspace_id,name,created_at) VALUES(?,?,?,?)",
                (project_id, workspace_id, project_name, now),
            )

    def register_profiles(self, registry: ProfileRegistry) -> None:
        now = utc_now_ts()
        profiles = []
        for profile_id in ("generic", "novel"):
            profile = registry.get(profile_id, "1.0.0")
            profiles.append((profile.profile_id, profile.version, profile.domain, json_dumps({
                "allowed_memory_types": sorted(profile.allowed_memory_types),
                "allowed_subtypes": sorted(profile.allowed_subtypes),
                "indexable_fields": sorted(profile.indexable_fields),
            }), now))
        with self.transaction() as connection:
            connection.executemany(
                """INSERT OR IGNORE INTO domain_profiles(profile_id,version,domain,config_json,created_at)
                   VALUES(?,?,?,?,?)""",
                profiles,
            )

    def insert_source(
        self,
        *,
        source_id: str,
        workspace_id: str,
        project_id: str,
        visibility: str,
        owner_principal_id: str | None,
        source_path: str,
        source_sha256: str,
        source_type: str,
        sensitivity: str,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        own_connection = connection is None
        connection = connection or self.connect()
        try:
            connection.execute(
                """INSERT INTO sources(source_id,workspace_id,project_id,visibility,owner_principal_id,
                   source_path,source_sha256,source_type,sensitivity,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (source_id, workspace_id, project_id, visibility, owner_principal_id,
                 source_path, source_sha256, source_type, sensitivity, utc_now_ts()),
            )
            if own_connection:
                connection.commit()
        finally:
            if own_connection:
                connection.close()

    def insert_source_unit(
        self,
        *,
        source_unit_id: str,
        source_id: str,
        project_id: str | None = None,
        parent_unit_id: str | None,
        scope_type: str,
        scope_ref: str,
        source_unit_hash: str,
        text_content: str,
        metadata: dict[str, Any] | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        own_connection = connection is None
        connection = connection or self.connect()
        try:
            if project_id is None:
                source = connection.execute(
                    "SELECT project_id FROM sources WHERE source_id=?", (source_id,)
                ).fetchone()
                if source is None:
                    raise ValueError("source not found while deriving source unit project_id")
                project_id = source["project_id"]
            if not project_id or not project_id.strip():
                raise ValueError("source unit project_id must be explicit and non-empty")
            connection.execute(
                """INSERT INTO source_units(source_unit_id,source_id,project_id,parent_unit_id,scope_type,
                   scope_ref,source_unit_hash,text_content,metadata_json,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (source_unit_id, source_id, project_id, parent_unit_id, scope_type, scope_ref,
                 source_unit_hash, text_content, json_dumps(metadata or {}), utc_now_ts()),
            )
            if own_connection:
                connection.commit()
        finally:
            if own_connection:
                connection.close()

    def get_source(self, source_id: str) -> dict[str, Any] | None:
        with self.read() as connection:
            row = connection.execute("SELECT * FROM sources WHERE source_id=?", (source_id,)).fetchone()
            return dict(row) if row else None

    def get_source_unit(self, source_unit_id: str) -> dict[str, Any] | None:
        with self.read() as connection:
            row = connection.execute(
                "SELECT * FROM source_units WHERE source_unit_id=?", (source_unit_id,)
            ).fetchone()
            return dict(row) if row else None

    def save_grant(self, grant: AccessGrant) -> None:
        with self.transaction() as connection:
            connection.execute(
                """INSERT OR REPLACE INTO access_grants(
                   grant_id,principal_id,workspace_id,scope_json,actions_json,expires_at,
                   remaining_queries,purpose,revoked,revoked_at,revoked_reason,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    grant.grant_id, grant.principal_id, grant.workspace_id,
                    json_dumps(grant.scope.model_dump(mode="json")),
                    json_dumps(list(grant.actions)), dt_to_ts(grant.expires_at),
                    grant.remaining_queries, grant.purpose, int(grant.revoked),
                    dt_to_ts(grant.revoked_at), grant.revoked_reason, dt_to_ts(grant.created_at),
                ),
            )

    def get_grant(self, grant_id: str) -> AccessGrant | None:
        with self.read() as connection:
            row = connection.execute("SELECT * FROM access_grants WHERE grant_id=?", (grant_id,)).fetchone()
            return self._row_to_grant(row) if row else None

    @staticmethod
    def _row_to_grant(row: sqlite3.Row) -> AccessGrant:
        return AccessGrant(
            grant_id=row["grant_id"],
            principal_id=row["principal_id"],
            workspace_id=row["workspace_id"],
            scope=AccessScope.model_validate(json_loads(row["scope_json"], {})),
            actions=json_loads(row["actions_json"], []),
            expires_at=ts_to_dt(row["expires_at"]),
            remaining_queries=row["remaining_queries"],
            purpose=row["purpose"],
            revoked=bool(row["revoked"]),
            revoked_at=ts_to_dt(row["revoked_at"]),
            revoked_reason=row["revoked_reason"],
            created_at=ts_to_dt(row["created_at"]),
        )

    def register_token_jti(self, *, jti: str, principal_id: str, grant_id: str, expires_at: datetime) -> bool:
        with self.transaction() as connection:
            try:
                connection.execute(
                    "INSERT INTO token_replay_cache(jti,principal_id,grant_id,expires_at) VALUES(?,?,?,?)",
                    (jti, principal_id, grant_id, dt_to_ts(expires_at)),
                )
            except sqlite3.IntegrityError:
                return False
        return True

    def consume_query_budget(self, grant_id: str) -> int | None:
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT remaining_queries FROM access_grants WHERE grant_id=?", (grant_id,)
            ).fetchone()
            if not row:
                raise KeyError(f"unknown grant: {grant_id}")
            remaining = row["remaining_queries"]
            if remaining is None:
                return None
            if remaining <= 0:
                raise PermissionError("query budget exhausted")
            connection.execute(
                "UPDATE access_grants SET remaining_queries=remaining_queries-1 WHERE grant_id=?",
                (grant_id,),
            )
            return remaining - 1

    def find_active_content(
        self,
        *,
        workspace_id: str,
        owner_principal_id: str,
        domain_profile: str,
        content_hash: str,
        normalized_subject: str,
        visibility: str,
        connection: sqlite3.Connection,
    ) -> dict[str, Any] | None:
        if visibility == "private":
            row = connection.execute(
                """SELECT * FROM memory_items
                   WHERE workspace_id=? AND owner_principal_id=? AND domain_profile=?
                     AND content_hash=? AND normalized_subject=?
                     AND status='active' AND deleted_at IS NULL
                     AND dedup_policy='content_only' AND visibility='private'
                   LIMIT 1""",
                (workspace_id, owner_principal_id, domain_profile, content_hash, normalized_subject),
            ).fetchone()
        else:
            row = connection.execute(
                """SELECT * FROM memory_items
                   WHERE workspace_id=? AND domain_profile=? AND content_hash=?
                     AND normalized_subject=? AND status='active' AND deleted_at IS NULL
                     AND dedup_policy='content_only' AND visibility IN ('workspace','shared')
                   LIMIT 1""",
                (workspace_id, domain_profile, content_hash, normalized_subject),
            ).fetchone()
        return dict(row) if row else None

    def insert_memory_pending(self, values: dict[str, Any], *, connection: sqlite3.Connection) -> None:
        columns = ",".join(values)
        placeholders = ",".join("?" for _ in values)
        connection.execute(f"INSERT INTO memory_items({columns}) VALUES({placeholders})", tuple(values.values()))

    def insert_evidence(self, memory_id: str, evidence: MemoryEvidence, *, connection: sqlite3.Connection) -> None:
        connection.execute(
            """INSERT INTO memory_evidence(evidence_id,memory_id,source_unit_id,quote,
               locator_json,evidence_status,created_at) VALUES(?,?,?,?,?,?,?)""",
            (evidence.evidence_id, memory_id, evidence.source_unit_id, evidence.quote,
             json_dumps(evidence.locator_json), evidence.status.value, utc_now_ts()),
        )

    def activate_memory(self, memory_id: str, *, connection: sqlite3.Connection) -> None:
        connection.execute(
            "UPDATE memory_items SET status='active',updated_at=? WHERE memory_id=?",
            (utc_now_ts(), memory_id),
        )

    def get_memory(self, memory_id: str) -> dict[str, Any] | None:
        with self.read() as connection:
            row = connection.execute("SELECT * FROM memory_items WHERE memory_id=?", (memory_id,)).fetchone()
            return dict(row) if row else None

    def get_memory_record(self, memory_id: str) -> MemoryRecord | None:
        with self.read() as connection:
            row = connection.execute(
                """SELECT m.*,s.source_sha256,su.source_unit_hash
                   FROM memory_items m JOIN sources s ON s.source_id=m.source_id
                   JOIN source_units su ON su.source_unit_id=m.source_unit_id
                   WHERE m.memory_id=?""",
                (memory_id,),
            ).fetchone()
            if not row:
                return None
            evidence_rows = connection.execute(
                "SELECT * FROM memory_evidence WHERE memory_id=? ORDER BY evidence_id", (memory_id,)
            ).fetchall()
            evidences = [
                MemoryEvidence(
                    evidence_id=e["evidence_id"], source_unit_id=e["source_unit_id"],
                    quote=e["quote"], locator_json=json_loads(e["locator_json"], {}),
                    status=EvidenceStatus(e["evidence_status"]),
                ) for e in evidence_rows
            ]
            return self._row_to_memory_record(row, evidences)

    @staticmethod
    def _row_to_memory_record(row: sqlite3.Row, evidences: list[MemoryEvidence]) -> MemoryRecord:
        return MemoryRecord(
            memory_id=row["memory_id"], workspace_id=row["workspace_id"], project_id=row["project_id"],
            task_id=row["task_id"], conversation_id=row["conversation_id"],
            owner_principal_id=row["owner_principal_id"], domain_profile=row["domain_profile"],
            profile_version=row["profile_version"], memory_type=row["memory_type"],
            subtype=row["subtype"], subject=row["subject"], normalized_subject=row["normalized_subject"],
            entity_type=row["entity_type"], entity_id=row["entity_id"], content=row["content"],
            content_hash=row["content_hash"], content_hash_version=row["content_hash_version"],
            dedup_policy=row["dedup_policy"], status=MemoryStatus(row["status"]),
            importance=row["importance"], sensitivity=row["sensitivity"], visibility=row["visibility"],
            assertion_source=row["assertion_source"], verification_state=row["verification_state"],
            effective_from=ts_to_dt(row["effective_from"]), effective_to=ts_to_dt(row["effective_to"]),
            source_id=row["source_id"], source_unit_id=row["source_unit_id"],
            source_sha256=row["source_sha256"], source_unit_hash=row["source_unit_hash"],
            source_scope=SourceScope.model_validate(json_loads(row["source_scope_json"], {})),
            confidence=float(row["confidence"]), supersedes_id=row["supersedes_id"],
            version=int(row["version"]), domain_payload=json_loads(row["domain_payload_json"], {}),
            evidences=evidences, deleted_at=ts_to_dt(row["deleted_at"]), delete_reason=row["delete_reason"],
            created_at=ts_to_dt(row["created_at"]), updated_at=ts_to_dt(row["updated_at"]),
        )

    def insert_idempotency(
        self,
        *,
        workspace_id: str,
        principal_id: str,
        idempotency_key: str,
        operation_type: str,
        request_sha256: str,
        operation_id: str,
        result: dict[str, Any],
        status: str,
        connection: sqlite3.Connection,
    ) -> None:
        connection.execute(
            """INSERT INTO idempotency_records(
               workspace_id,principal_id,idempotency_key,operation_type,request_sha256,
               operation_id,result_json,status,created_at) VALUES(?,?,?,?,?,?,?,?,?)""",
            (workspace_id, principal_id, idempotency_key, operation_type, request_sha256,
             operation_id, json_dumps(result), status, utc_now_ts()),
        )

    def get_idempotency(self, workspace_id: str, principal_id: str, key: str) -> dict[str, Any] | None:
        with self.read() as connection:
            row = connection.execute(
                """SELECT * FROM idempotency_records
                   WHERE workspace_id=? AND principal_id=? AND idempotency_key=?""",
                (workspace_id, principal_id, key),
            ).fetchone()
            return dict(row) if row else None

    def insert_outbox(
        self,
        *,
        outbox_id: str,
        memory_id: str,
        workspace_id: str,
        target_type: str,
        target_name: str,
        operation: str,
        item_version: int,
        index_schema_version: str,
        embedding_model: str | None,
        embedding_dim: int | None,
        connection: sqlite3.Connection,
    ) -> bool:
        now = utc_now_ts()
        cursor = connection.execute(
            """INSERT OR IGNORE INTO memory_outbox(
               outbox_id,memory_id,workspace_id,target_type,target_name,operation,
               item_version,index_schema_version,embedding_model,embedding_dim,status,
               next_retry_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,'pending',?,?,?)""",
            (outbox_id, memory_id, workspace_id, target_type, target_name, operation,
             item_version, index_schema_version, embedding_model, embedding_dim,
             now, now, now),
        )
        return cursor.rowcount == 1

    def claim_outbox(
        self,
        *,
        owner: str,
        limit: int,
        lease_seconds: int,
        now: int | None = None,
        outbox_ids: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        now = now or utc_now_ts()
        if outbox_ids is not None and not outbox_ids:
            return []
        with self.transaction() as connection:
            connection.execute(
                """UPDATE memory_outbox SET status='pending',lease_owner=NULL,lease_expires_at=NULL,
                   updated_at=? WHERE status='claimed' AND lease_expires_at < ?""",
                (now, now),
            )
            id_filter = ""
            params: list[Any] = []
            if outbox_ids is not None:
                placeholders = ",".join("?" for _ in outbox_ids)
                id_filter = f" AND outbox_id IN ({placeholders})"
                params.extend(outbox_ids)
            rows = connection.execute(
                "SELECT * FROM memory_outbox WHERE status='pending' AND next_retry_at<=?"
                + id_filter
                + " ORDER BY created_at,outbox_id LIMIT ?",
                (now, *params, limit),
            ).fetchall()
            ids = [row["outbox_id"] for row in rows]
            if not ids:
                return []
            placeholders = ",".join("?" for _ in ids)
            connection.execute(
                f"""UPDATE memory_outbox SET status='claimed',lease_owner=?,lease_expires_at=?,updated_at=?
                    WHERE outbox_id IN ({placeholders})""",
                (owner, now + lease_seconds, now, *ids),
            )
            claimed = connection.execute(
                f"SELECT * FROM memory_outbox WHERE outbox_id IN ({placeholders}) ORDER BY created_at,outbox_id",
                tuple(ids),
            ).fetchall()
            return [dict(row) for row in claimed]

    def mark_outbox_done(self, outbox_id: str, *, connection: sqlite3.Connection | None = None) -> None:
        own = connection is None
        connection = connection or self.connect()
        try:
            connection.execute(
                "UPDATE memory_outbox SET status='done',lease_owner=NULL,lease_expires_at=NULL,updated_at=? WHERE outbox_id=?",
                (utc_now_ts(), outbox_id),
            )
            if own:
                connection.commit()
        finally:
            if own:
                connection.close()

    def fail_outbox(self, outbox_id: str, *, error: str, max_retries: int = 5, now: int | None = None) -> str:
        now = now or utc_now_ts()
        with self.transaction() as connection:
            row = connection.execute("SELECT * FROM memory_outbox WHERE outbox_id=?", (outbox_id,)).fetchone()
            if not row:
                raise KeyError(outbox_id)
            retry_count = int(row["retry_count"]) + 1
            if retry_count > max_retries:
                status, next_retry = "poison", now
            else:
                status, next_retry = "pending", now + min(3600, 2 ** retry_count)
            connection.execute(
                """UPDATE memory_outbox SET status=?,retry_count=?,last_error=?,next_retry_at=?,
                   lease_owner=NULL,lease_expires_at=NULL,updated_at=? WHERE outbox_id=?""",
                (status, retry_count, error[:2000], next_retry, now, outbox_id),
            )
            return status

    def resume_poison_outbox(self, outbox_id: str, *, now: int | None = None) -> None:
        now = now or utc_now_ts()
        with self.transaction() as connection:
            cursor = connection.execute(
                """UPDATE memory_outbox SET status='pending',retry_count=0,last_error=NULL,
                   next_retry_at=?,lease_owner=NULL,lease_expires_at=NULL,updated_at=?
                   WHERE outbox_id=? AND status='poison'""",
                (now, now, outbox_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("outbox event is not poison")

    def get_outbox(self, outbox_id: str) -> dict[str, Any] | None:
        with self.read() as connection:
            row = connection.execute("SELECT * FROM memory_outbox WHERE outbox_id=?", (outbox_id,)).fetchone()
            return dict(row) if row else None

    def sync_fts_memory(self, memory_id: str, *, connection: sqlite3.Connection, force_rebuild: bool = False) -> bool:
        row = connection.execute(
            """SELECT m.*,su.text_content AS unit_text,s.source_path
               FROM memory_items m JOIN source_units su ON su.source_unit_id=m.source_unit_id
               JOIN sources s ON s.source_id=m.source_id WHERE m.memory_id=?""",
            (memory_id,),
        ).fetchone()
        if not row:
            return False
        sync = connection.execute("SELECT * FROM memory_fts_sync_state WHERE memory_id=?", (memory_id,)).fetchone()
        fts_rows = connection.execute(
            "SELECT content FROM memory_fts WHERE memory_id=?", (memory_id,)
        ).fetchall()
        if sync and not force_rebuild:
            sync_version = int(sync["item_version"])
            item_version = int(row["version"])
            if sync_version > item_version:
                return False
            if sync_version == item_version:
                if row["deleted_at"] is not None:
                    if int(sync["deleted"]) == 1 and not fts_rows:
                        return False
                elif (
                    int(sync["deleted"]) == 0
                    and len(fts_rows) == 1
                    and fts_rows[0]["content"] == row["content"]
                ):
                    return False
        connection.execute("DELETE FROM memory_fts WHERE memory_id=?", (memory_id,))
        deleted = 1 if row["deleted_at"] is not None else 0
        if deleted == 0:
            connection.execute(
                """INSERT INTO memory_fts(
                   memory_id,workspace_id,project_id,task_id,conversation_id,
                   owner_principal_id,domain_profile,memory_type,status,sensitivity,
                   visibility,content) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (row["memory_id"], row["workspace_id"], row["project_id"], row["task_id"] or "",
                 row["conversation_id"] or "", row["owner_principal_id"], row["domain_profile"],
                 row["memory_type"], row["status"], row["sensitivity"], row["visibility"], row["content"]),
            )
        connection.execute(
            """INSERT INTO memory_fts_sync_state(memory_id,item_version,deleted,updated_at)
               VALUES(?,?,?,?) ON CONFLICT(memory_id) DO UPDATE SET
               item_version=excluded.item_version,deleted=excluded.deleted,updated_at=excluded.updated_at""",
            (memory_id, int(row["version"]), deleted, utc_now_ts()),
        )
        return True

    def fts_search(
        self, query: str, *, limit: int = 300, chapter_indices: list[int] | None = None
    ) -> list[dict[str, Any]]:
        term = '"' + query.replace('"', '""') + '"'
        chapter_sql = ""
        params: list[Any] = [term]
        if chapter_indices:
            placeholders = ",".join("?" for _ in chapter_indices)
            chapter_sql = (
                " AND CAST(json_extract(m.domain_payload_json,'$.chapter_index') AS INTEGER)"
                f" IN ({placeholders})"
            )
            params.extend(chapter_indices)
        params.append(limit)
        with self.read() as connection:
            rows = connection.execute(
                f"""SELECT f.*,bm25(memory_fts) AS rank_score
                    FROM memory_fts f JOIN memory_items m ON m.memory_id=f.memory_id
                    WHERE memory_fts MATCH ?{chapter_sql}
                    ORDER BY rank LIMIT ?""",
                params,
            ).fetchall()
            return [dict(row) for row in rows]

    def substring_search(
        self, query: str, *, workspace_ids: Sequence[str], project_ids: Sequence[str] = (),
        domains: Sequence[str] = (), memory_types: Sequence[str] = (),
        statuses: Sequence[MemoryStatus] = (MemoryStatus.ACTIVE,),
        max_sensitivity: SensitivityLevel = SensitivityLevel.PRIVATE,
        principal_id: str = "", allow_global_kb: bool = False,
        limit: int = 300, chapter_indices: list[int] | None = None,
    ) -> list[dict[str, Any]]:
        """Restricted SQLite substring fallback for 1-2 CJK characters.

        This path deliberately applies the same tenant/owner/domain/sensitivity
        restrictions as the normal search gateway before returning candidate IDs.
        """
        if not query or len(query) > 2:
            raise ValueError("substring fallback is limited to 1-2 characters")
        allowed_sensitivities = [
            level.value for level in SensitivityLevel
            if SENSITIVITY_ORDER[level] <= SENSITIVITY_ORDER[max_sensitivity]
        ]
        workspaces = list(workspace_ids)
        if allow_global_kb and "global_kb" not in workspaces:
            workspaces.append("global_kb")
        backslash = chr(92)
        conditions = [
            f"f.content LIKE ? ESCAPE '{backslash}'",
            f"m.workspace_id IN ({','.join('?' for _ in workspaces)})",
            f"m.status IN ({','.join('?' for _ in statuses)})",
            f"m.sensitivity IN ({','.join('?' for _ in allowed_sensitivities)})",
            "(m.visibility != 'private' OR m.owner_principal_id = ?)",
        ]
        escaped = query.replace(backslash, backslash * 2).replace("%", backslash + "%").replace("_", backslash + "_")
        params: list[Any] = [f"%{escaped}%", *workspaces, *[status.value for status in statuses], *allowed_sensitivities, principal_id]
        if project_ids:
            conditions.append(f"m.project_id IN ({','.join('?' for _ in project_ids)})")
            params.extend(project_ids)
        if domains:
            conditions.append(f"m.domain_profile IN ({','.join('?' for _ in domains)})")
            params.extend(domains)
        if memory_types:
            conditions.append(f"m.memory_type IN ({','.join('?' for _ in memory_types)})")
            params.extend(memory_types)
        if chapter_indices:
            conditions.append(
                "CAST(json_extract(m.domain_payload_json,'$.chapter_index') AS INTEGER)"
                f" IN ({','.join('?' for _ in chapter_indices)})"
            )
            params.extend(chapter_indices)
        params.append(limit)
        with self.read() as connection:
            rows = connection.execute(
                f"""SELECT f.*, 0.0 AS rank_score
                    FROM memory_fts f JOIN memory_items m ON m.memory_id=f.memory_id
                    WHERE {' AND '.join(conditions)}
                    ORDER BY m.updated_at DESC, m.memory_id LIMIT ?""",
                params,
            ).fetchall()
            return [dict(row) for row in rows]

    def subject_search(
        self, normalized_subject: str, *, limit: int = 300,
        chapter_indices: list[int] | None = None,
    ) -> list[dict[str, Any]]:
        chapter_sql = ""
        params: list[Any] = [normalized_subject]
        if chapter_indices:
            placeholders = ",".join("?" for _ in chapter_indices)
            chapter_sql = (
                " AND CAST(json_extract(domain_payload_json,'$.chapter_index') AS INTEGER)"
                f" IN ({placeholders})"
            )
            params.extend(chapter_indices)
        params.append(limit)
        with self.read() as connection:
            rows = connection.execute(
                f"""SELECT m.* FROM memory_items m WHERE m.normalized_subject=?
                    AND m.deleted_at IS NULL{chapter_sql}
                    ORDER BY m.updated_at DESC LIMIT ?""",
                params,
            ).fetchall()
            return [dict(row) for row in rows]

    def list_active_records(self, *, non_secret_only: bool = False) -> list[MemoryRecord]:
        sql = "SELECT memory_id FROM memory_items WHERE status='active' AND deleted_at IS NULL"
        if non_secret_only:
            sql += " AND sensitivity <> 'secret'"
        sql += " ORDER BY memory_id"
        with self.read() as connection:
            ids = [row["memory_id"] for row in connection.execute(sql)]
        return [record for memory_id in ids if (record := self.get_memory_record(memory_id)) is not None]

    def delete_memory_rows(self, memory_ids: Sequence[str], *, delete_reason: str, connection: sqlite3.Connection) -> None:
        now = utc_now_ts()
        for memory_id in memory_ids:
            connection.execute(
                """UPDATE memory_items SET deleted_at=?,delete_reason=?,version=version+1,updated_at=?
                   WHERE memory_id=? AND deleted_at IS NULL""",
                (now, delete_reason, now, memory_id),
            )

    def select_delete_targets(
        self, *, workspace_id: str, target_type: str, target_id: str,
        principal_id: str, connection: sqlite3.Connection,
    ) -> list[dict[str, Any]]:
        if target_type == "memory_id":
            sql = "SELECT * FROM memory_items WHERE workspace_id=? AND memory_id=? AND deleted_at IS NULL"
            params = (workspace_id, target_id)
        elif target_type == "conversation_id":
            sql = "SELECT * FROM memory_items WHERE workspace_id=? AND conversation_id=? AND deleted_at IS NULL"
            params = (workspace_id, target_id)
        elif target_type == "workspace_id":
            sql = "SELECT * FROM memory_items WHERE workspace_id=? AND deleted_at IS NULL"
            params = (workspace_id,)
        else:
            raise ValueError(f"unsupported delete target: {target_type}")
        rows = []
        for row in connection.execute(sql, params):
            if row["visibility"] == "private" and row["owner_principal_id"] != principal_id:
                continue
            rows.append(dict(row))
        return rows

    def insert_query_audit(self, values: dict[str, Any]) -> None:
        with self.transaction() as connection:
            connection.execute(
                """INSERT INTO query_audits(
                   query_id,principal_id,token_jti,grant_id,workspace_id,query_hash,
                   redacted_query,max_sensitivity,filters_json,retrieved_ids_json,
                   answer_summary,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (values["query_id"], values["principal_id"], values["token_jti"],
                 values["grant_id"], values["workspace_id"], values["query_hash"],
                 values["redacted_query"], values["max_sensitivity"], json_dumps(values["filters_json"]),
                 json_dumps(values["retrieved_ids_json"]), values["answer_summary"],
                 int(values.get("created_at", utc_now_ts()))),
            )

    def get_query_audit(self, query_id: str) -> dict[str, Any] | None:
        with self.read() as connection:
            row = connection.execute("SELECT * FROM query_audits WHERE query_id=?", (query_id,)).fetchone()
            return dict(row) if row else None

    def reconciliation_counts(self, *, workspace_id: str | None = None) -> dict[str, int]:
        workspace_sql = " AND workspace_id=?" if workspace_id else ""
        workspace_params = (workspace_id,) if workspace_id else ()
        with self.read() as connection:
            active_total = connection.execute(
                "SELECT count(*) FROM memory_items WHERE status='active' AND deleted_at IS NULL"
                + workspace_sql,
                workspace_params,
            ).fetchone()[0]
            active_non_secret = connection.execute(
                "SELECT count(*) FROM memory_items WHERE status='active' AND deleted_at IS NULL"
                " AND sensitivity <> 'secret'" + workspace_sql,
                workspace_params,
            ).fetchone()[0]
            fts_rows = connection.execute(
                "SELECT count(*) FROM memory_fts WHERE 1=1" + workspace_sql,
                workspace_params,
            ).fetchone()[0]
            fts_sync_rows = connection.execute(
                "SELECT count(*) FROM memory_fts_sync_state"
                + (" WHERE memory_id IN (SELECT memory_id FROM memory_items WHERE workspace_id=?)"
                   if workspace_id else ""),
                workspace_params,
            ).fetchone()[0]
        return {
            "active_total": int(active_total),
            "active_non_secret": int(active_non_secret),
            "fts_rows": int(fts_rows),
            "fts_sync_rows": int(fts_sync_rows),
        }

    def cleanup_access_grants(
        self,
        *,
        retention_seconds: int = 7 * 86400,
        now: int | None = None,
    ) -> int:
        """Delete expired or revoked grants after a bounded audit-retention window."""

        if isinstance(retention_seconds, bool) or retention_seconds < 0:
            raise ValueError("retention_seconds must be a non-negative integer")
        current = now or utc_now_ts()
        cutoff = current - retention_seconds
        with self.transaction() as connection:
            cursor = connection.execute(
                """DELETE FROM access_grants
                   WHERE (revoked=1 AND COALESCE(revoked_at,expires_at)<=?)
                      OR expires_at<=?""",
                (cutoff, cutoff),
            )
            connection.execute("DELETE FROM token_replay_cache WHERE expires_at<=?", (current,))
            return int(cursor.rowcount)

    def cleanup_ttl(
        self,
        *,
        now: int | None = None,
        tombstone_retention_seconds: int = 30 * 86400,
        token_replay_grace_seconds: int = 30,
    ) -> dict[str, int]:
        if tombstone_retention_seconds < 0:
            raise ValueError("tombstone_retention_seconds must be non-negative")
        if token_replay_grace_seconds < 0:
            raise ValueError("token_replay_grace_seconds must be non-negative")
        now = now or utc_now_ts()
        tombstone_cutoff = now - tombstone_retention_seconds
        with self.transaction() as connection:
            working_memory = connection.execute(
                "DELETE FROM working_memory WHERE ttl_expires_at <= ?", (now,)
            ).rowcount
            tombstone_ids = [
                row["memory_id"] for row in connection.execute(
                    """SELECT m.memory_id FROM memory_items m
                       WHERE m.deleted_at IS NOT NULL AND m.deleted_at <= ?
                       AND NOT EXISTS (
                         SELECT 1 FROM memory_items child
                         WHERE child.supersedes_id = m.memory_id
                       )
                       ORDER BY m.memory_id""",
                    (tombstone_cutoff,),
                ).fetchall()
            ]
            for memory_id in tombstone_ids:
                connection.execute("DELETE FROM memory_evidence WHERE memory_id=?", (memory_id,))
                connection.execute(
                    "DELETE FROM memory_links WHERE from_memory_id=? OR to_memory_id=?",
                    (memory_id, memory_id),
                )
                connection.execute("DELETE FROM memory_fts WHERE memory_id=?", (memory_id,))
                connection.execute("DELETE FROM memory_fts_sync_state WHERE memory_id=?", (memory_id,))
                connection.execute("DELETE FROM memory_items WHERE memory_id=?", (memory_id,))
            token_replays = connection.execute(
                "DELETE FROM token_replay_cache WHERE expires_at <= ?",
                (now - token_replay_grace_seconds,),
            ).rowcount
            return {
                "working_memory": working_memory,
                "tombstones": len(tombstone_ids),
                "token_replays": token_replays,
            }
