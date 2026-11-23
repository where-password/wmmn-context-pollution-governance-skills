from __future__ import annotations

import uuid
from collections import Counter
from typing import Any

from .db import SQLiteStore, dt_to_ts, utc_now_ts
from .fakes import FakeEmbedding, FakeVectorIndex
from .models import MemoryIndexDoc, MemoryStatus, SensitivityLevel
from .profiles import ProfileRegistry


OUTBOX_EMBEDDING_MODEL = "fake-bge-m3-v1"
OUTBOX_EMBEDDING_DIM = 1024
OUTBOX_INDEX_SCHEMA_VERSION = "memory-index-v1"


class OutboxWorker:
    def __init__(
        self, *, store: SQLiteStore, profiles: ProfileRegistry,
        embedding: FakeEmbedding, vector_index: FakeVectorIndex,
        write_alias: str | None = None,
        index_schema_version: str = OUTBOX_INDEX_SCHEMA_VERSION,
        embedding_model_override: str | None = None,
        embedding_dim_override: int | None = None,
        index_schema_version_override: str | None = None,
        max_retries: int = 5,
    ) -> None:
        self.store = store
        self.profiles = profiles
        self.embedding = embedding
        self.vector_index = vector_index
        self.write_alias = write_alias or getattr(vector_index, "write_alias", None)
        if not self.write_alias:
            raise ValueError("OutboxWorker requires a configured write_alias")
        self.index_schema_version = index_schema_version
        self.embedding_model_override = embedding_model_override
        self.embedding_dim_override = embedding_dim_override
        self.index_schema_version_override = index_schema_version_override
        self.max_retries = max_retries

    def process_claimed(self, events: list[dict[str, Any]]) -> dict[str, int]:
        counts: Counter[str] = Counter()
        for event in events:
            try:
                counts[self.process_event(event)] += 1
            except Exception as exc:
                status = self.store.fail_outbox(
                    event["outbox_id"], error=f"{type(exc).__name__}: {exc}", max_retries=self.max_retries
                )
                counts[status] += 1
        return dict(counts)

    def run_once(self, *, owner: str, limit: int = 100, lease_seconds: int = 60) -> dict[str, int]:
        events = self.store.claim_outbox(owner=owner, limit=limit, lease_seconds=lease_seconds)
        return self.process_claimed(events) if events else {}

    def run_specific(
        self,
        *,
        outbox_ids: list[str],
        owner: str,
        limit: int = 100,
        lease_seconds: int = 60,
    ) -> dict[str, int]:
        events = self.store.claim_outbox(
            owner=owner,
            limit=limit,
            lease_seconds=lease_seconds,
            outbox_ids=outbox_ids,
        )
        return self.process_claimed(events) if events else {}

    def process_event(self, event: dict[str, Any]) -> str:
        current = self.store.get_memory(event["memory_id"])
        if current and int(event["item_version"]) < int(current["version"]):
            # A delete may carry the old indexed version while canonical is already superseded.
            # It is still safe to remove the obsolete index row because the memory is no longer active.
            if not (event["operation"] == "delete" and current["status"] != "active"):
                self.store.mark_outbox_done(event["outbox_id"])
                return "stale"
        if current and current["deleted_at"] is not None and event["operation"] in {"upsert", "reconcile"}:
            self.store.mark_outbox_done(event["outbox_id"])
            return "deleted_skip"
        if event["target_type"] == "fts":
            changed = self._process_fts(event)
            self.store.mark_outbox_done(event["outbox_id"])
            return "fts_synced" if changed else "fts_unchanged"
        if event["target_type"] == "milvus":
            if current is None and event["operation"] != "delete":
                self.store.mark_outbox_done(event["outbox_id"])
                return "missing"
            self._process_vector(event)
            self.store.mark_outbox_done(event["outbox_id"])
            return "vector_synced"
        raise ValueError(f"unsupported outbox target: {event['target_type']}")

    def _process_fts(self, event: dict[str, Any]) -> bool:
        with self.store.transaction() as connection:
            authoritative = connection.execute(
                "SELECT * FROM memory_items WHERE memory_id=?", (event["memory_id"],)
            ).fetchone()
            if authoritative is not None and int(event["item_version"]) < int(authoritative["version"]):
                if not (event["operation"] == "delete" and authoritative["status"] != "active"):
                    return False
            if (
                event["operation"] == "delete"
                or authoritative is None
                or authoritative["status"] != "active"
                or authoritative["deleted_at"] is not None
            ):
                connection.execute("DELETE FROM memory_fts WHERE memory_id=?", (event["memory_id"],))
                connection.execute("DELETE FROM memory_fts_sync_state WHERE memory_id=?", (event["memory_id"],))
                return True
            return self.store.sync_fts_memory(event["memory_id"], connection=connection, force_rebuild=False)

    def _vector_target(self, event: dict[str, Any]) -> str:
        if event["target_type"] == "milvus" and event["operation"] in {"upsert", "delete"}:
            return self.write_alias
        return event["target_name"]

    def _process_vector(self, event: dict[str, Any]) -> None:
        target = self._vector_target(event)
        allow_physical = event["operation"] == "reconcile"
        if event["operation"] == "delete":
            self.vector_index.delete(
                [event["memory_id"]], target=target, allow_physical=allow_physical
            )
            return
        record = self.store.get_memory_record(event["memory_id"])
        if record is None:
            if event["operation"] == "delete":
                self.vector_index.delete(
                    [event["memory_id"]], target=target, allow_physical=allow_physical
                )
            return
        if record.status != MemoryStatus.ACTIVE or record.deleted_at is not None:
            self.vector_index.delete(
                [record.memory_id], target=target, allow_physical=allow_physical
            )
            return
        if record.sensitivity == SensitivityLevel.SECRET:
            return
        profile = self.profiles.get(record.domain_profile, record.profile_version)
        embedding_text = profile.embedding_text(_SyntheticWriteItem(record), record.normalized_subject)
        embedding = self.embedding.embed_one(embedding_text)
        doc = MemoryIndexDoc(
            memory_id=record.memory_id, embedding=embedding,
            workspace_id=record.workspace_id, project_id=record.project_id,
            task_id=record.task_id or "", conversation_id=record.conversation_id or "",
            owner_principal_id=record.owner_principal_id, domain_profile=record.domain_profile,
            profile_version=record.profile_version, memory_type=record.memory_type,
            subtype=record.subtype or "", subject=record.subject or "",
            normalized_subject=record.normalized_subject, status=record.status,
            importance=record.importance, sensitivity=record.sensitivity,
            visibility=record.visibility,
            effective_from_ts=dt_to_ts(record.effective_from) or 0,
            effective_to_ts=dt_to_ts(record.effective_to) or 9_223_372_036_854_775_807,
            version=record.version,
            embedding_model=self.embedding_model_override or event.get("embedding_model") or self.embedding.model,
            embedding_dim=int(
                self.embedding_dim_override or event.get("embedding_dim") or self.embedding.dimension
            ),
            normalization="l2", index_schema_version=self.index_schema_version_override or event["index_schema_version"],
            collection_alias=target, canonical_text=record.content,
            embedding_text=embedding_text,
            source_scope_json=record.source_scope.model_dump(mode="json"),
            indexed_payload_json=profile.extract_indexable_payload(record.domain_payload),
            created_at_ts=dt_to_ts(record.created_at) or utc_now_ts(),
        )
        self.vector_index.upsert([doc], target=target, allow_physical=allow_physical)

    def enqueue_reconcile(
        self, *, workspace_id: str, target_name: str, index_schema_version: str = "memory-index-v2"
    ) -> list[str]:
        records = [
            record for record in self.store.list_active_records(non_secret_only=True)
            if record.workspace_id == workspace_id
        ]
        ids: list[str] = []
        with self.store.transaction() as connection:
            for record in records:
                outbox_id = "out_" + uuid.uuid4().hex
                if self.store.insert_outbox(
                    outbox_id=outbox_id, memory_id=record.memory_id,
                    workspace_id=record.workspace_id, target_type="milvus",
                    target_name=target_name, operation="reconcile",
                    item_version=record.version, index_schema_version=index_schema_version,
                    embedding_model=self.embedding.model, embedding_dim=self.embedding.dimension,
                    connection=connection,
                ):
                    ids.append(outbox_id)
        return ids


class _SyntheticWriteItem:
    def __init__(self, record) -> None:
        self.memory_type = record.memory_type
        self.subtype = record.subtype
        self.content = record.content
        self.domain_payload = record.domain_payload
