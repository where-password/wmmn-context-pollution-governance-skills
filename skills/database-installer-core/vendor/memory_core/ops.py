from __future__ import annotations

import hashlib
import json
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .db import SQLiteStore, utc_now_ts


TEST_READ_ALIAS = "memory_read_alias_test_001"
TEST_WRITE_ALIAS = "memory_write_alias_test_001"
TEST_COLLECTION = "memory_core_stress105_v5_v1"


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _error_payload(exc: Exception) -> dict[str, str]:
    return {"type": type(exc).__name__, "message": str(exc)[:2000]}


def assert_test_resource_names(*, read_alias: str, write_alias: str, collection_name: str) -> None:
    expected = (TEST_READ_ALIAS, TEST_WRITE_ALIAS, TEST_COLLECTION)
    actual = (read_alias, write_alias, collection_name)
    if actual != expected:
        raise PermissionError(
            "runtime entry is restricted to the isolated test aliases and collection: "
            f"expected={expected!r}, actual={actual!r}"
        )


def assert_isolated_db_path(path: str | Path) -> Path:
    resolved = Path(path).resolve()
    allowed_root = Path(__file__).resolve().parents[2] / "work"
    if not resolved.is_relative_to(allowed_root.resolve()):
        raise PermissionError(f"runtime database must stay under isolated work directory: {allowed_root}")
    if resolved.name == "stress_105_v5.sqlite3" and resolved.parent.name == "sqlite":
        raise PermissionError("source SQLite is read-only and cannot be used by runtime entry")
    return resolved


class JsonRunRecorder:
    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def write(self, kind: str, result: dict[str, Any]) -> None:
        payload = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
        (self.directory / f"latest_{kind}_run.json").write_text(
            payload, encoding="utf-8", newline="\n"
        )
        with (self.directory / f"{kind}_runs.jsonl").open(
            "a", encoding="utf-8", newline="\n"
        ) as handle:
            handle.write(json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n")


class OutboxAdmin:
    def __init__(self, store: SQLiteStore) -> None:
        self.store = store

    def status_counts(self) -> dict[str, int]:
        with self.store.read() as connection:
            rows = connection.execute(
                "SELECT status,count(*) AS count FROM memory_outbox GROUP BY status ORDER BY status"
            ).fetchall()
        return {row["status"]: int(row["count"]) for row in rows}

    def target_status_counts(self) -> dict[str, dict[str, int]]:
        with self.store.read() as connection:
            rows = connection.execute(
                """SELECT target_type,status,count(*) AS count FROM memory_outbox
                   GROUP BY target_type,status ORDER BY target_type,status"""
            ).fetchall()
        result: dict[str, dict[str, int]] = {}
        for row in rows:
            result.setdefault(row["target_type"], {})[row["status"]] = int(row["count"])
        return result

    def list_poison(self, *, limit: int = 100) -> list[dict[str, Any]]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        with self.store.read() as connection:
            rows = connection.execute(
                """SELECT * FROM memory_outbox WHERE status='poison'
                   ORDER BY updated_at,outbox_id LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def resume_poison(self, outbox_id: str, *, now: int | None = None) -> dict[str, Any]:
        self.store.resume_poison_outbox(outbox_id, now=now)
        event = self.store.get_outbox(outbox_id)
        if event is None:
            raise KeyError(outbox_id)
        return event


class ReconcileService:
    """Plan, apply and verify collection-level FTS/Milvus reconciliation."""

    def __init__(
        self,
        *,
        store: SQLiteStore,
        vector_index: Any,
        workspace_id: str | None = None,
        domain_profile: str | None = None,
        worker: Any | None = None,
        embedding_model: str | None = None,
        embedding_dimension: int | None = None,
        index_schema_version: str | None = None,
        page_size: int = 1000,
    ) -> None:
        if page_size <= 0:
            raise ValueError("page_size must be positive")
        self.store = store
        self.vector_index = vector_index
        self.workspace_id = workspace_id
        self.domain_profile = domain_profile
        self.worker = worker
        self.embedding_model = embedding_model
        self.embedding_dimension = embedding_dimension
        self.index_schema_version = index_schema_version
        self.page_size = page_size

    @staticmethod
    def _milvus_filter(workspace_id: str | None, domain_profile: str | None = None) -> str:
        filters: list[str] = []
        if workspace_id:
            escaped = workspace_id.replace("\\", "\\\\").replace('"', '\\"')
            filters.append(f'workspace_id == "{escaped}"')
        if domain_profile:
            escaped = domain_profile.replace("\\", "\\\\").replace('"', '\\"')
            filters.append(f'domain_profile == "{escaped}"')
        return " and ".join(filters)

    @staticmethod
    def _fingerprint(text: str) -> str:
        return hashlib.sha256((text or "").encode("utf-8")).hexdigest()

    def _resolve_expected_metadata(self) -> dict[str, Any]:
        model = self.embedding_model
        dimension = self.embedding_dimension
        schema = self.index_schema_version
        if model is None or dimension is None or schema is None:
            with self.store.read() as connection:
                row = connection.execute(
                    """SELECT embedding_model,embedding_dim,index_schema_version
                       FROM memory_outbox
                       WHERE target_type='milvus'
                       ORDER BY updated_at DESC, outbox_id DESC LIMIT 1"""
                ).fetchone()
            if row:
                model = model or row["embedding_model"]
                dimension = dimension or row["embedding_dim"]
                schema = schema or row["index_schema_version"]
        model = model or getattr(self.vector_index, "embedding_model", None)
        dimension = dimension or getattr(self.vector_index, "dimension", None)
        schema = schema or getattr(self.vector_index, "index_schema_version", None)
        return {"embedding_model": model, "embedding_dimension": dimension, "index_schema_version": schema}

    def _snapshot(self) -> dict[str, Any]:
        where = ["status='active'", "deleted_at IS NULL"]
        params: list[Any] = []
        if self.workspace_id:
            where.append("workspace_id=?")
            params.append(self.workspace_id)
        if self.domain_profile:
            where.append("domain_profile=?")
            params.append(self.domain_profile)
        sql = (
            """SELECT memory_id,workspace_id,project_id,owner_principal_id,domain_profile,
                      memory_type,subtype,content,content_hash,sensitivity,visibility,
                      version,updated_at
               FROM memory_items WHERE """
            + " AND ".join(where)
            + " ORDER BY memory_id"
        )
        with self.store.read() as connection:
            rows = [dict(row) for row in connection.execute(sql, params)]
        snapshot_hash_input = [
            {
                "memory_id": row["memory_id"],
                "version": int(row["version"]),
                "content_hash": row["content_hash"],
                "workspace_id": row["workspace_id"],
                "project_id": row["project_id"],
                "domain_profile": row["domain_profile"],
                "sensitivity": row["sensitivity"],
            }
            for row in rows
        ]
        snapshot_id = hashlib.sha256(
            json.dumps(snapshot_hash_input, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        watermark = max((int(row["updated_at"]) for row in rows), default=0)
        return {
            "snapshot_id": snapshot_id,
            "snapshot_watermark": watermark,
            "snapshot_item_count": len(rows),
            "rows": rows,
        }

    def _actual_fts(self) -> list[dict[str, Any]]:
        where = ["1=1"]
        params: list[Any] = []
        if self.workspace_id:
            where.append("f.workspace_id=?")
            params.append(self.workspace_id)
        if self.domain_profile:
            where.append("f.domain_profile=?")
            params.append(self.domain_profile)
        sql = (
            """SELECT f.memory_id,f.workspace_id,f.project_id,f.domain_profile,f.content,
                      s.item_version,s.deleted
               FROM memory_fts f
               LEFT JOIN memory_fts_sync_state s ON s.memory_id=f.memory_id
               WHERE """
            + " AND ".join(where)
            + " ORDER BY f.memory_id"
        )
        with self.store.read() as connection:
            return [dict(row) for row in connection.execute(sql, params)]

    def _actual_milvus(self) -> list[dict[str, Any]]:
        iterator = getattr(self.vector_index, "iter_rows", None)
        if iterator is None:
            raise RuntimeError("vector index must provide iter_rows for collection-level reconciliation")
        rows = list(iterator(
            filter=self._milvus_filter(self.workspace_id, self.domain_profile),
            batch_size=self.page_size,
            output_fields=[
                "memory_id", "workspace_id", "project_id", "owner_principal_id",
                "domain_profile", "memory_type", "status", "sensitivity", "visibility",
                "version", "index_schema_version", "embedding_model", "embedding_dim",
                "canonical_text",
            ],
        ))
        filtered = []
        for row in rows:
            if self.workspace_id and row.get("workspace_id") != self.workspace_id:
                continue
            if self.domain_profile and row.get("domain_profile") != self.domain_profile:
                continue
            filtered.append(row)
        return filtered

    @staticmethod
    def _diff_entry(
        resource: str, difference_type: str, *, expected: dict[str, Any] | None = None,
        actual: dict[str, Any] | None = None, detail: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        row = expected or actual or {}
        return {
            "resource": resource,
            "difference_type": difference_type,
            "memory_id": row.get("memory_id"),
            "workspace_id": row.get("workspace_id"),
            "expected_version": int(expected["version"]) if expected and expected.get("version") is not None else None,
            "actual_version": int(actual["version"]) if actual and actual.get("version") is not None else None,
            "expected_content_hash": expected.get("content_hash") if expected else None,
            "detail": detail or {},
        }

    def _diff_fts(self, expected_rows: list[dict[str, Any]], actual_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        expected = {row["memory_id"]: row for row in expected_rows}
        actual = {row["memory_id"]: row for row in actual_rows}
        diffs: list[dict[str, Any]] = []
        for memory_id, exp in expected.items():
            act = actual.get(memory_id)
            if act is None:
                diffs.append(self._diff_entry("fts", "missing", expected=exp))
                continue
            if act.get("item_version") is None or int(act["item_version"]) != int(exp["version"]):
                diffs.append(self._diff_entry("fts", "stale_version", expected=exp, actual=act))
            if self._fingerprint(act.get("content", "")) != self._fingerprint(exp.get("content", "")):
                diffs.append(self._diff_entry("fts", "fingerprint_mismatch", expected=exp, actual=act))
        for memory_id, act in actual.items():
            if memory_id not in expected:
                diffs.append(self._diff_entry("fts", "extra_index_id", actual=act))
        return diffs

    def _diff_milvus(
        self, expected_rows: list[dict[str, Any]], actual_rows: list[dict[str, Any]],
        expected_metadata: dict[str, Any],
    ) -> list[dict[str, Any]]:
        expected = {row["memory_id"]: row for row in expected_rows}
        actual = {row["memory_id"]: row for row in actual_rows}
        diffs: list[dict[str, Any]] = []
        for memory_id, exp in expected.items():
            act = actual.get(memory_id)
            if act is None:
                diffs.append(self._diff_entry("milvus", "missing", expected=exp))
                continue
            if act.get("version") is None or int(act["version"]) != int(exp["version"]):
                diffs.append(self._diff_entry("milvus", "stale_version", expected=exp, actual=act))
            if self._fingerprint(act.get("canonical_text", "")) != self._fingerprint(exp.get("content", "")):
                diffs.append(self._diff_entry("milvus", "fingerprint_mismatch", expected=exp, actual=act))
            metadata_detail = {}
            if expected_metadata["index_schema_version"] and act.get("index_schema_version") != expected_metadata["index_schema_version"]:
                metadata_detail["index_schema_version"] = {
                    "expected": expected_metadata["index_schema_version"], "actual": act.get("index_schema_version"),
                }
            if expected_metadata["embedding_model"] and act.get("embedding_model") != expected_metadata["embedding_model"]:
                metadata_detail["embedding_model"] = {
                    "expected": expected_metadata["embedding_model"], "actual": act.get("embedding_model"),
                }
            if expected_metadata["embedding_dimension"] and int(act.get("embedding_dim") or 0) != int(expected_metadata["embedding_dimension"]):
                metadata_detail["embedding_dimension"] = {
                    "expected": expected_metadata["embedding_dimension"], "actual": act.get("embedding_dim"),
                }
            if metadata_detail:
                diffs.append(self._diff_entry("milvus", "metadata_mismatch", expected=exp, actual=act, detail=metadata_detail))
        for memory_id, act in actual.items():
            if memory_id not in expected:
                diffs.append(self._diff_entry("milvus", "extra_index_id", actual=act))
        return diffs

    @staticmethod
    def _operations_for_differences(diffs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        operations: dict[tuple[str, str, str], dict[str, Any]] = {}
        for diff in diffs:
            resource = diff["resource"]
            memory_id = diff["memory_id"]
            is_extra = diff["difference_type"] == "extra_index_id"
            operation = "delete" if is_extra else "upsert"
            key = (resource, memory_id, operation)
            existing = operations.setdefault(key, {
                "resource": resource,
                "memory_id": memory_id,
                "workspace_id": diff.get("workspace_id"),
                "operation": operation,
                "planned_item_version": diff["actual_version"] if is_extra else diff["expected_version"],
                "planned_content_hash": None if is_extra else diff.get("expected_content_hash"),
                "reasons": [],
            })
            if diff["difference_type"] not in existing["reasons"]:
                existing["reasons"].append(diff["difference_type"])
        return sorted(operations.values(), key=lambda item: (item["resource"], item["memory_id"], item["operation"]))

    def plan(self) -> dict[str, Any]:
        snapshot = self._snapshot()
        expected_metadata = self._resolve_expected_metadata()
        expected_fts = snapshot["rows"]
        expected_milvus = [row for row in snapshot["rows"] if row["sensitivity"] != "secret"]
        actual_fts = self._actual_fts()
        actual_milvus = self._actual_milvus()
        fts_diffs = self._diff_fts(expected_fts, actual_fts)
        milvus_diffs = self._diff_milvus(expected_milvus, actual_milvus, expected_metadata)
        diffs = fts_diffs + milvus_diffs
        operations = self._operations_for_differences(diffs)
        resources = {
            "fts": {
                "snapshot_id": snapshot["snapshot_id"],
                "snapshot_watermark": snapshot["snapshot_watermark"],
                "snapshot_item_count": snapshot["snapshot_item_count"],
                "expected_count": len(expected_fts),
                "actual_count": len(actual_fts),
                "differences": fts_diffs,
            },
            "milvus": {
                "snapshot_id": snapshot["snapshot_id"],
                "snapshot_watermark": snapshot["snapshot_watermark"],
                "snapshot_item_count": snapshot["snapshot_item_count"],
                "expected_count": len(expected_milvus),
                "actual_count": len(actual_milvus),
                "differences": milvus_diffs,
            },
        }
        issues = []
        for resource, data in resources.items():
            if data["differences"]:
                issues.append({
                    "resource": resource,
                    "expected": data["expected_count"],
                    "actual": data["actual_count"],
                    "difference": data["actual_count"] - data["expected_count"],
                    "difference_count": len(data["differences"]),
                })
        material = {
            "snapshot_id": snapshot["snapshot_id"],
            "workspace_id": self.workspace_id,
            "domain_profile": self.domain_profile,
            "collection": getattr(self.vector_index, "collection_name", None),
            "read_alias": getattr(self.vector_index, "read_alias", None),
            "write_alias": getattr(self.vector_index, "write_alias", None),
            "expected_metadata": expected_metadata,
            "differences": diffs,
            "operations": operations,
        }
        plan_id = hashlib.sha256(
            json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return {
            "plan_id": plan_id,
            "generated_at_utc": _utc_iso(),
            "snapshot_id": snapshot["snapshot_id"],
            "snapshot_watermark": snapshot["snapshot_watermark"],
            "snapshot_item_count": snapshot["snapshot_item_count"],
            "workspace_id": self.workspace_id,
            "domain_profile": self.domain_profile,
            "collection": getattr(self.vector_index, "collection_name", None),
            "read_alias": getattr(self.vector_index, "read_alias", None),
            "write_alias": getattr(self.vector_index, "write_alias", None),
            "expected_metadata": expected_metadata,
            "expected_counts": {"fts": len(expected_fts), "milvus": len(expected_milvus)},
            "actual_counts": {"fts": len(actual_fts), "milvus": len(actual_milvus)},
            "resources": resources,
            "differences": diffs,
            "operations": operations,
            "issues": issues,
            "passed": not diffs,
            "report_only": True,
            "deletions_performed": 0,
        }

    def run(self) -> dict[str, Any]:
        return self.plan()

    @staticmethod
    def _prepare_existing_outbox_for_replay(
        connection,
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
    ) -> tuple[str, str]:
        row = connection.execute(
            """SELECT * FROM memory_outbox
               WHERE memory_id=? AND target_type=? AND target_name=? AND item_version=?
                 AND index_schema_version=? AND operation=?
               ORDER BY created_at,outbox_id LIMIT 1""",
            (memory_id, target_type, target_name, item_version, index_schema_version, operation),
        ).fetchone()
        if row is None:
            row = connection.execute(
                "SELECT * FROM memory_outbox WHERE outbox_id=?", (outbox_id,)
            ).fetchone()
        if row is None:
            raise RuntimeError("outbox insert was ignored but no matching event exists")
        expected = (memory_id, workspace_id, target_type, target_name, operation, item_version)
        actual = (
            row["memory_id"], row["workspace_id"], row["target_type"], row["target_name"],
            row["operation"], int(row["item_version"]),
        )
        if actual != expected:
            raise RuntimeError(f"outbox replay event collision: expected={expected!r}, actual={actual!r}")
        if row["status"] == "claimed":
            return str(row["outbox_id"]), "claimed"
        now = utc_now_ts()
        connection.execute(
            """UPDATE memory_outbox SET status='pending',retry_count=0,last_error=NULL,
               next_retry_at=?,lease_owner=NULL,lease_expires_at=NULL,
               embedding_model=?,embedding_dim=?,updated_at=? WHERE outbox_id=?""",
            (now, embedding_model, embedding_dim, now, row["outbox_id"]),
        )
        return str(row["outbox_id"]), "replayed"

    def apply(
        self,
        plan: dict[str, Any],
        *,
        explicit: bool = False,
        worker: Any | None = None,
        owner: str = "reconcile-worker",
        limit: int = 1000,
        lease_seconds: int = 60,
    ) -> dict[str, Any]:
        if not explicit:
            raise PermissionError("reconcile apply requires explicit=True")
        if plan.get("plan_id") is None or plan.get("snapshot_id") is None:
            raise ValueError("invalid reconcile plan")
        applied: list[str] = []
        replayed: list[str] = []
        replay_states: dict[str, str] = {}
        skipped: list[dict[str, Any]] = []
        expected_metadata = plan.get("expected_metadata") or {}
        schema_version = expected_metadata.get("index_schema_version") or "memory-index-v1"
        with self.store.transaction() as connection:
            for operation in plan.get("operations", []):
                memory_id = operation["memory_id"]
                current = connection.execute(
                    """SELECT memory_id,workspace_id,content_hash,status,version,deleted_at
                       FROM memory_items WHERE memory_id=?""",
                    (memory_id,),
                ).fetchone()
                planned_version = int(operation.get("planned_item_version") or 0)
                planned_hash = operation.get("planned_content_hash")
                workspace_id = (
                    operation.get("workspace_id")
                    or (current["workspace_id"] if current is not None else None)
                    or self.workspace_id
                    or ""
                )
                if not workspace_id:
                    raise ValueError(f"workspace_id is required for reconcile operation: {memory_id}")
                if operation["operation"] == "upsert":
                    if (
                        current is None
                        or current["status"] != "active"
                        or current["deleted_at"] is not None
                        or int(current["version"]) != planned_version
                        or (planned_hash is not None and current["content_hash"] != planned_hash)
                    ):
                        skipped.append({**operation, "skip_reason": "skip_stale_plan"})
                        continue
                else:
                    if current is not None and current["status"] == "active" and current["deleted_at"] is None:
                        skipped.append({**operation, "skip_reason": "skip_stale_plan"})
                        continue
                target_name = "memory_fts" if operation["resource"] == "fts" else "memory_write_alias"
                item_version = planned_version
                outbox_id = "out_" + hashlib.sha256(
                    f"{memory_id}|{operation['resource']}|{target_name}|{item_version}|{schema_version}|{operation['operation']}".encode("utf-8")
                ).hexdigest()[:32]
                inserted = self.store.insert_outbox(
                    outbox_id=outbox_id,
                    memory_id=memory_id,
                    workspace_id=workspace_id,
                    target_type=operation["resource"],
                    target_name=target_name,
                    operation=operation["operation"],
                    item_version=item_version,
                    index_schema_version=schema_version,
                    embedding_model=expected_metadata.get("embedding_model") if operation["resource"] == "milvus" else None,
                    embedding_dim=expected_metadata.get("embedding_dimension") if operation["resource"] == "milvus" else None,
                    connection=connection,
                )
                if inserted:
                    applied.append(outbox_id)
                else:
                    actual_outbox_id, replay_state = self._prepare_existing_outbox_for_replay(
                        connection,
                        outbox_id=outbox_id,
                        memory_id=memory_id,
                        workspace_id=workspace_id,
                        target_type=operation["resource"],
                        target_name=target_name,
                        operation=operation["operation"],
                        item_version=item_version,
                        index_schema_version=schema_version,
                        embedding_model=(
                            expected_metadata.get("embedding_model")
                            if operation["resource"] == "milvus" else None
                        ),
                        embedding_dim=(
                            expected_metadata.get("embedding_dimension")
                            if operation["resource"] == "milvus" else None
                        ),
                    )
                    replayed.append(actual_outbox_id)
                    replay_states[actual_outbox_id] = replay_state
        worker = worker or self.worker
        worker_result = {}
        selected_ids = applied + replayed
        if worker is not None and selected_ids:
            run_specific = getattr(worker, "run_specific", None)
            if run_specific is not None:
                worker_result = run_specific(
                    outbox_ids=selected_ids,
                    owner=owner,
                    limit=max(limit, len(selected_ids)),
                    lease_seconds=lease_seconds,
                )
            else:
                worker_result = worker.run_once(
                    owner=owner,
                    limit=max(limit, len(selected_ids)),
                    lease_seconds=lease_seconds,
                )
        selected_statuses: dict[str, str] = {}
        if selected_ids:
            placeholders = ",".join("?" for _ in selected_ids)
            with self.store.read() as connection:
                rows = connection.execute(
                    f"SELECT outbox_id,status FROM memory_outbox WHERE outbox_id IN ({placeholders})",
                    tuple(selected_ids),
                ).fetchall()
            selected_statuses = {row["outbox_id"]: row["status"] for row in rows}
        return {
            "plan_id": plan["plan_id"],
            "snapshot_id": plan["snapshot_id"],
            "applied_count": len(applied),
            "replayed_count": len(replayed),
            "skipped_count": len(skipped),
            "applied_outbox_ids": applied,
            "replayed_outbox_ids": replayed,
            "replay_states": replay_states,
            "selected_outbox_statuses": selected_statuses,
            "skipped": skipped,
            "worker_result": worker_result,
        }

    def verify(self, plan: dict[str, Any]) -> dict[str, Any]:
        current = self.plan()
        snapshot_matches = current["snapshot_id"] == plan.get("snapshot_id")
        return {
            "plan_id": plan.get("plan_id"),
            "verify_plan_id": current["plan_id"],
            "snapshot_id": plan.get("snapshot_id"),
            "verify_snapshot_id": current["snapshot_id"],
            "snapshot_matches": snapshot_matches,
            "passed": current["passed"] and snapshot_matches,
            "expected_counts": current["expected_counts"],
            "actual_counts": current["actual_counts"],
            "differences": current["differences"],
        }
class SchedulerService:
    def __init__(
        self,
        *,
        worker: Any,
        store: SQLiteStore,
        reconciler: ReconcileService,
        recorder: JsonRunRecorder | None = None,
    ) -> None:
        self.worker = worker
        self.store = store
        self.reconciler = reconciler
        self.recorder = recorder

    @staticmethod
    def _capture(stage: str, operation: Callable[[], Any]) -> dict[str, Any]:
        try:
            return {"stage": stage, "status": "ok", "result": operation(), "error": None}
        except Exception as exc:
            return {
                "stage": stage,
                "status": "error",
                "result": None,
                "error": _error_payload(exc),
            }

    def run_once(
        self,
        *,
        owner: str,
        limit: int = 100,
        lease_seconds: int = 60,
        tombstone_retention_seconds: int = 30 * 86400,
        token_replay_grace_seconds: int = 30,
    ) -> dict[str, Any]:
        started_at = _utc_iso()
        started_monotonic = time.monotonic()
        worker_stage = self._capture(
            "outbox_worker",
            lambda: self.worker.run_once(owner=owner, limit=limit, lease_seconds=lease_seconds),
        )
        ttl_stage = self._capture(
            "ttl_cleanup",
            lambda: self.store.cleanup_ttl(
                now=utc_now_ts(),
                tombstone_retention_seconds=tombstone_retention_seconds,
                token_replay_grace_seconds=token_replay_grace_seconds,
            ),
        )
        reconcile_stage = self._capture("reconcile", self.reconciler.run)
        errors = [
            stage["error"]
            for stage in (worker_stage, ttl_stage, reconcile_stage)
            if stage["status"] == "error"
        ]
        worker_result = worker_stage.get("result") or {}
        reconcile_result = reconcile_stage.get("result") or {}
        if errors:
            status = "error"
        elif int(worker_result.get("poison", 0)) > 0 or reconcile_result.get("passed") is False:
            status = "degraded"
        else:
            status = "ok"
        result = {
            "run_id": f"scheduler-{utc_now_ts()}-{time.time_ns() % 1_000_000_000}",
            "kind": "scheduler",
            "status": status,
            "started_at_utc": started_at,
            "finished_at_utc": _utc_iso(),
            "elapsed_ms": round((time.monotonic() - started_monotonic) * 1000, 3),
            "owner": owner,
            "worker": worker_stage,
            "ttl_cleanup": ttl_stage,
            "reconcile": reconcile_stage,
            "errors": errors,
        }
        if self.recorder is not None:
            self.recorder.write("scheduler", result)
        return result


class WorkerRunRecorder:
    def __init__(self, recorder: JsonRunRecorder) -> None:
        self.recorder = recorder

    def run_once(self, worker: Any, *, owner: str, limit: int, lease_seconds: int) -> dict[str, Any]:
        started_at = _utc_iso()
        started_monotonic = time.monotonic()
        try:
            result = worker.run_once(owner=owner, limit=limit, lease_seconds=lease_seconds)
            payload = {
                "kind": "worker",
                "status": "degraded" if int(result.get("poison", 0)) else "ok",
                "started_at_utc": started_at,
                "finished_at_utc": _utc_iso(),
                "elapsed_ms": round((time.monotonic() - started_monotonic) * 1000, 3),
                "owner": owner,
                "result": result,
                "error": None,
            }
        except Exception as exc:
            payload = {
                "kind": "worker",
                "status": "error",
                "started_at_utc": started_at,
                "finished_at_utc": _utc_iso(),
                "elapsed_ms": round((time.monotonic() - started_monotonic) * 1000, 3),
                "owner": owner,
                "result": None,
                "error": _error_payload(exc),
            }
        self.recorder.write("worker", payload)
        return payload


def summarize_stage_status(result: dict[str, Any]) -> Counter[str]:
    statuses: Counter[str] = Counter()
    for key in ("worker", "ttl_cleanup", "reconcile"):
        stage = result.get(key)
        if isinstance(stage, dict):
            statuses[str(stage.get("status"))] += 1
    return statuses
