from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .db import SQLiteStore, json_dumps, utc_now_ts
from .fakes import FakeEmbedding, FakeVectorIndex
from .models import EvidenceStatus, MemoryEvidence, MemoryIndexDoc, SourceScope
from .outbox import _SyntheticWriteItem
from .profiles import ProfileRegistry


WORKSPACE_ID = "stress_test_001"
PROJECT_ID = "novel_stress_105_v5"
MIGRATION_PRINCIPAL = "migration:stress-105-v5"
PROFILE_ID = "novel"
PROFILE_VERSION = "1.0.0"
INDEX_SCHEMA_VERSION = "memory-index-v1"
MIGRATION_ID = "migrate:stress-105-v5"

CONTENT_TYPE_MAP: dict[str, tuple[str, str, str]] = {
    "narrative_fact": ("fact", "narrative_fact", "content_only"),
    "character_relation": ("relationship", "character_relation", "content_only"),
    "plot_decision": ("decision", "plot_decision", "content_only"),
    "timeline_event": ("event", "timeline_event", "no_auto_merge"),
    "unresolved_thread": ("open_question", "unresolved_thread", "content_only"),
    "world_rule": ("fact", "world_rule", "content_only"),
}


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def copy_source_database(source: str | Path, target: str | Path, *, overwrite: bool = False) -> dict[str, Any]:
    source = Path(source).resolve()
    target = Path(target).resolve()
    if source == target:
        raise ValueError("source and target databases must differ")
    if not source.is_file():
        raise FileNotFoundError(source)
    if target.exists() and not overwrite:
        raise FileExistsError(f"migration target already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    source_hash_before = sha256_file(source)
    if target.exists():
        target.unlink()
    shutil.copy2(source, target)
    source_hash_after = sha256_file(source)
    if source_hash_before != source_hash_after:
        raise RuntimeError("source database changed during copy")
    return {
        "source": str(source), "target": str(target), "source_sha256": source_hash_after,
        "target_size": target.stat().st_size,
    }


def migrate_stress_105_v5_copy(
    *, source: str | Path, target: str | Path, overwrite: bool = False,
    embedding: FakeEmbedding | None = None, vector_index: FakeVectorIndex | None = None,
) -> dict[str, Any]:
    copy_info = copy_source_database(source, target, overwrite=overwrite)
    target = Path(target).resolve()
    source_hash_before = copy_info["source_sha256"]
    profiles = ProfileRegistry()
    profile = profiles.get(PROFILE_ID, PROFILE_VERSION)
    store = SQLiteStore(target)
    store.migrate()
    store.ensure_workspace_project(
        workspace_id=WORKSPACE_ID, workspace_name="Novel Stress Test",
        project_id=PROJECT_ID, project_name="Stress 105 V5",
    )
    store.register_profiles(profiles)
    embedding = embedding or FakeEmbedding(dimension=1024)
    vector_index = vector_index or FakeVectorIndex()

    with sqlite3.connect(target) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        legacy_source = connection.execute("SELECT * FROM etl_sources_v1 LIMIT 1").fetchone()
        if legacy_source is None:
            raise RuntimeError("legacy source row not found")
        facts = [dict(row) for row in connection.execute(
            """SELECT f.*,m.redacted_text,m.metadata_json,m.source_local_order
               FROM facts f JOIN messages m ON m.message_id=f.anchor_message_id
               ORDER BY f.anchor_turn_id,m.source_local_order,f.fact_id"""
        )]
        if len(facts) != 1254:
            raise RuntimeError(f"expected 1254 source facts, found {len(facts)}")
        now = utc_now_ts()
        connection.execute("BEGIN IMMEDIATE")
        try:
            source_rows: dict[str, dict[str, Any]] = {}
            unit_rows: dict[str, dict[str, Any]] = {}
            for fact in facts:
                metadata = json.loads(fact["metadata_json"])
                turn_id = fact["anchor_turn_id"]
                source_id = f"src_{turn_id}"
                unit_id = f"unit_{fact['anchor_message_id']}"
                source_rows[source_id] = {
                    "turn_id": turn_id, "chapter_index": metadata.get("chapter_index"),
                    "chapter_title": metadata.get("chapter_title"),
                }
                unit_rows[unit_id] = {
                    "source_id": source_id, "message_id": fact["anchor_message_id"],
                    "turn_id": turn_id, "text": fact["redacted_text"],
                    "source_local_order": fact["source_local_order"], "metadata": metadata,
                }
            for source_id, value in sorted(source_rows.items()):
                connection.execute(
                    """INSERT OR IGNORE INTO sources(
                       source_id,workspace_id,project_id,visibility,owner_principal_id,source_path,
                       source_sha256,source_type,sensitivity,created_at)
                       VALUES(?,?,?,'workspace',NULL,?,?,'novel_turn','private',?)""",
                    (source_id, WORKSPACE_ID, PROJECT_ID,
                     f"{legacy_source['uri']}#turn={value['turn_id']}",
                     legacy_source["sha256"], now),
                )
            for unit_id, value in sorted(unit_rows.items()):
                connection.execute(
                    """INSERT OR IGNORE INTO source_units(
                       source_unit_id,source_id,project_id,parent_unit_id,scope_type,scope_ref,
                       source_unit_hash,text_content,metadata_json,created_at)
                       VALUES(?,?,?,NULL,'chat_turn',?,?,?,?,?)""",
                    (unit_id, value["source_id"], PROJECT_ID, value["message_id"], sha256_text(value["text"]),
                     value["text"], json_dumps({
                         "message_id": value["message_id"], "turn_id": value["turn_id"],
                         "source_local_order": value["source_local_order"],
                         "chapter_index": value["metadata"].get("chapter_index"),
                         "chapter_title": value["metadata"].get("chapter_title"),
                     }), now),
                )

            for fact in facts:
                source_unit_id = f"unit_{fact['anchor_message_id']}"
                source_id = f"src_{fact['anchor_turn_id']}"
                memory_type, subtype, dedup_policy = CONTENT_TYPE_MAP[fact["content_type"]]
                content_hash = hashlib.sha256(
                    f"v1|{PROFILE_ID}||{profile.normalize_content(fact['canonical_text'])}".encode("utf-8")
                ).hexdigest()
                memory_id = f"mem_{fact['fact_id'].removeprefix('fact_')}"
                scope = SourceScope(
                    scope_type="chat_turn", ref=fact["anchor_message_id"],
                    locator_json={
                        "message_id": fact["anchor_message_id"], "turn_id": fact["anchor_turn_id"],
                        "source_local_order": fact["source_local_order"],
                    },
                )
                metadata = json.loads(fact["metadata_json"])
                payload = {
                    "original_fact_id": fact["fact_id"], "content_type": fact["content_type"],
                    "memory_category": fact["memory_category"], "importance": fact["importance"],
                    "retention_reason": fact["retention_reason"], "story_function": fact["story_function"],
                    "assertion_polarity": fact["assertion_polarity"], "is_fragment": bool(fact["is_fragment"]),
                    "supporting_message_ids": json.loads(fact["supporting_message_ids_json"] or "[]"),
                    "anchor_turn_ids": json.loads(fact["anchor_turn_ids_json"] or "[]"),
                    "context_turn_ids": json.loads(fact["context_turn_ids_json"] or "[]"),
                    "batch_id": fact["batch_id"], "turn_start": fact["turn_start"], "turn_end": fact["turn_end"],
                    "chapter_index": metadata.get("chapter_index"), "chapter_title": metadata.get("chapter_title"),
                }
                idempotency_key = f"{MIGRATION_ID}:{fact['fact_id']}"
                connection.execute(
                    """INSERT OR IGNORE INTO memory_items(
                       memory_id,workspace_id,project_id,task_id,conversation_id,owner_principal_id,
                       domain_profile,profile_version,memory_type,subtype,subject,normalized_subject,
                       entity_type,entity_id,content,content_hash,content_hash_version,dedup_policy,
                       status,importance,sensitivity,visibility,assertion_source,verification_state,
                       effective_from,effective_to,source_id,
                       source_unit_id,source_scope_json,confidence,supersedes_id,version,
                       domain_payload_json,idempotency_key,deleted_at,delete_reason,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (memory_id, WORKSPACE_ID, PROJECT_ID, None, None, MIGRATION_PRINCIPAL,
                     PROFILE_ID, PROFILE_VERSION, memory_type, subtype, None, "", None, None,
                     fact["canonical_text"], content_hash, "v1", dedup_policy,
                     "pending_review", fact["importance"], "private", "workspace",
                     "primary_source", "source_verified", None, None,
                     source_id, source_unit_id, json_dumps(scope.model_dump(mode="json")),
                     0.9, None, 1, json_dumps(payload), idempotency_key, None, None,
                     int(fact["created_at"]), int(fact["created_at"])),
                )
                evidence_id = f"ev_{fact['fact_id'].removeprefix('fact_')}"
                connection.execute(
                    """INSERT OR IGNORE INTO memory_evidence(
                       evidence_id,memory_id,source_unit_id,quote,locator_json,evidence_status,created_at)
                       VALUES(?,?,?,?,?,'verified',?)""",
                    (evidence_id, memory_id, source_unit_id, fact["evidence_quote"],
                     json_dumps(scope.locator_json), int(fact["created_at"])),
                )
                connection.execute(
                    "UPDATE memory_items SET status='active',updated_at=? WHERE memory_id=? AND status='pending_review'",
                    (int(fact["created_at"]), memory_id),
                )
                connection.execute(
                    """INSERT OR IGNORE INTO idempotency_records(
                       workspace_id,principal_id,idempotency_key,operation_type,request_sha256,
                       operation_id,result_json,status,created_at) VALUES(?,?,?,?,?,?,?,?,?)""",
                    (WORKSPACE_ID, MIGRATION_PRINCIPAL, idempotency_key, "write",
                     sha256_text(fact["fact_id"]), MIGRATION_ID,
                     json_dumps({"memory_id": memory_id, "status": "active", "version": 1}),
                     "completed", int(fact["created_at"])),
                )
                for target_type, target_name in (("fts", "memory_fts"), ("milvus", "memory_write_alias")):
                    outbox_id = "out_" + sha256_text(
                        f"{memory_id}|{target_type}|{target_name}|1|{INDEX_SCHEMA_VERSION}|upsert"
                    )[:32]
                    connection.execute(
                        """INSERT OR IGNORE INTO memory_outbox(
                           outbox_id,memory_id,workspace_id,target_type,target_name,operation,
                           item_version,index_schema_version,embedding_model,embedding_dim,status,
                           retry_count,next_retry_at,created_at,updated_at)
                           VALUES(?,?,?,?,?,'upsert',1,?,?,?,'done',0,?,?,?)""",
                        (outbox_id, memory_id, WORKSPACE_ID, target_type, target_name,
                         INDEX_SCHEMA_VERSION, "fake-bge-m3-v1" if target_type == "milvus" else None,
                         1024 if target_type == "milvus" else None, now, now, now),
                    )
                store.sync_fts_memory(memory_id, connection=connection, force_rebuild=True)
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    records = store.list_active_records()
    documents = []
    for record in records:
        embedding_text = profile.embedding_text(_SyntheticWriteItem(record), record.normalized_subject)
        documents.append(MemoryIndexDoc(
            memory_id=record.memory_id, embedding=embedding.embed_one(embedding_text),
            workspace_id=record.workspace_id, project_id=record.project_id,
            task_id=record.task_id or "", conversation_id=record.conversation_id or "",
            owner_principal_id=record.owner_principal_id, domain_profile=record.domain_profile,
            profile_version=record.profile_version, memory_type=record.memory_type,
            subtype=record.subtype or "", subject=record.subject or "",
            normalized_subject=record.normalized_subject, status=record.status,
            importance=record.importance, sensitivity=record.sensitivity,
            visibility=record.visibility, version=record.version,
            embedding_model="fake-bge-m3-v1", embedding_dim=1024, normalization="l2",
            index_schema_version=INDEX_SCHEMA_VERSION, collection_alias="memory_write_alias",
            canonical_text=record.content, embedding_text=embedding_text,
            source_scope_json=record.source_scope.model_dump(mode="json"),
            indexed_payload_json=profile.extract_indexable_payload(record.domain_payload),
            created_at_ts=int(record.created_at.timestamp()),
        ))
    for start in range(0, len(documents), 100):
        vector_index.upsert(documents[start:start + 100], target="memory_write_alias")

    with sqlite3.connect(target) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
        active_count = connection.execute("SELECT count(*) FROM memory_items WHERE status='active' AND deleted_at IS NULL").fetchone()[0]
        evidence_count = connection.execute("SELECT count(*) FROM memory_evidence WHERE trim(quote) <> ''").fetchone()[0]
        private_count = connection.execute("SELECT count(*) FROM memory_items WHERE visibility='private'").fetchone()[0]
        workspace_count = connection.execute("SELECT count(*) FROM memory_items WHERE visibility='workspace'").fetchone()[0]
        source_count = connection.execute("SELECT count(*) FROM sources").fetchone()[0]
        unit_count = connection.execute("SELECT count(*) FROM source_units").fetchone()[0]
        fts_count = connection.execute("SELECT count(*) FROM memory_fts").fetchone()[0]
        outbox_done = connection.execute("SELECT count(*) FROM memory_outbox WHERE status='done'").fetchone()[0]
        original_types = {r["content_type"]: r["n"] for r in connection.execute("SELECT content_type,count(*) n FROM facts GROUP BY content_type")}
        mapped_types = {(r["memory_type"], r["subtype"]): r["n"] for r in connection.execute("SELECT memory_type,subtype,count(*) n FROM memory_items GROUP BY memory_type,subtype")}
        original_categories = {r["memory_category"]: r["n"] for r in connection.execute("SELECT memory_category,count(*) n FROM facts GROUP BY memory_category")}
        mapped_categories = Counter()
        for row in connection.execute("SELECT domain_payload_json FROM memory_items"):
            mapped_categories[json.loads(row["domain_payload_json"])["memory_category"]] += 1
        evidence_contains = 0
        for row in connection.execute(
            "SELECT e.quote,su.text_content FROM memory_evidence e JOIN source_units su ON su.source_unit_id=e.source_unit_id"
        ):
            evidence_contains += int(row["quote"] in row["text_content"])
    expected_type_counts = Counter()
    for content_type, count in original_types.items():
        memory_type, subtype, _ = CONTENT_TYPE_MAP[content_type]
        expected_type_counts[(memory_type, subtype)] += count
    mapped_type_counter = Counter(mapped_types)
    type_mismatches = (expected_type_counts - mapped_type_counter) + (mapped_type_counter - expected_type_counts)
    category_mismatches = Counter(original_categories) - mapped_categories
    category_mismatches += mapped_categories - Counter(original_categories)
    source_hash_after = sha256_file(Path(source))
    result = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(), "copy": copy_info,
        "source_unchanged": source_hash_before == source_hash_after,
        "source_hash_after": source_hash_after,
        "counts": {
            "source_facts": len(facts), "active_memory": active_count,
            "memory_evidence": evidence_count, "sources": source_count,
            "source_units": unit_count, "fts_rows": fts_count,
            "vector_rows": vector_index.count("memory_read_alias"),
            "outbox_done": outbox_done, "private": private_count, "workspace": workspace_count,
        },
        "mappings": {
            "source_content_type": original_types,
            "mapped_memory_type_subtype": {f"{k[0]}:{k[1]}": v for k, v in sorted(mapped_types.items())},
            "source_memory_category": original_categories,
            "mapped_memory_category": dict(sorted(mapped_categories.items())),
            "type_mismatches": dict(type_mismatches), "category_mismatches": dict(category_mismatches),
        },
        "integrity": {
            "sqlite_integrity_check": integrity, "foreign_key_violations": len(foreign_keys),
            "evidence_nonempty": evidence_count == 1254,
            "evidence_quote_contained": evidence_contains,
            "evidence_quote_contained_rate": evidence_contains / 1254,
        },
        "acceptance": {
            "active_1254": active_count == 1254,
            "evidence_nonempty_100pct": evidence_count == 1254 and evidence_contains == 1254,
            "fts_1254": fts_count == 1254, "vector_1254": vector_index.count("memory_read_alias") == 1254,
            "mapping_zero_mismatch": not type_mismatches and not category_mismatches,
            "workspace_private_isolation": private_count == 0 and workspace_count == 1254,
            "source_unchanged": source_hash_before == source_hash_after,
        },
    }
    result["acceptance"]["all_pass"] = all(result["acceptance"].values())
    return result
