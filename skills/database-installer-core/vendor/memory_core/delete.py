from __future__ import annotations

import hashlib
import json

from .db import SQLiteStore, utc_now_ts
from .models import AgentContextToken, MemoryDeleteInput, MemoryDeleteResult
from .security import TokenValidator


class MemoryDeleteError(RuntimeError):
    pass


class MemoryDeleteService:
    def __init__(self, *, store: SQLiteStore, token_validator: TokenValidator) -> None:
        self.store = store
        self.tokens = token_validator

    @staticmethod
    def _request_hash(item: MemoryDeleteInput) -> str:
        return hashlib.sha256(
            json.dumps(item.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    def delete(self, item: MemoryDeleteInput, token: AgentContextToken) -> MemoryDeleteResult:
        context = self.tokens.validate(token, action="delete", consume_query=False)
        if item.workspace_id != token.workspace_id or item.workspace_id != context.grant.workspace_id:
            raise PermissionError("delete workspace mismatch")
        request_hash = self._request_hash(item)
        existing = self.store.get_idempotency(item.workspace_id, token.principal_id, item.idempotency_key)
        if existing:
            if existing["request_sha256"] != request_hash:
                raise MemoryDeleteError("idempotency key reused with a different delete request")
            return MemoryDeleteResult.model_validate_json(existing["result_json"])

        with self.store.transaction() as connection:
            existing = connection.execute(
                """SELECT * FROM idempotency_records
                   WHERE workspace_id=? AND principal_id=? AND idempotency_key=?""",
                (item.workspace_id, token.principal_id, item.idempotency_key),
            ).fetchone()
            if existing:
                if existing["request_sha256"] != request_hash:
                    raise MemoryDeleteError("idempotency key reused with a different delete request")
                return MemoryDeleteResult.model_validate_json(existing["result_json"])
            targets = self.store.select_delete_targets(
                workspace_id=item.workspace_id, target_type=item.target_type,
                target_id=item.target_id, principal_id=token.principal_id, connection=connection,
            )
            if item.expected_version is not None:
                if item.target_type != "memory_id":
                    raise MemoryDeleteError("expected_version is only valid for memory_id delete")
                if len(targets) != 1 or int(targets[0]["version"]) != item.expected_version:
                    raise MemoryDeleteError("optimistic delete version check failed")
            memory_ids = [row["memory_id"] for row in targets]
            self.store.delete_memory_rows(memory_ids, delete_reason=item.delete_reason, connection=connection)
            outbox_ids: list[str] = []
            for row in targets:
                new_version = int(row["version"]) + 1
                for target_type, target_name in (("fts", "memory_fts"), ("milvus", "memory_write_alias")):
                    outbox_id = "out_" + hashlib.sha256(
                        f"{row['memory_id']}|{target_type}|{target_name}|{new_version}|memory-index-v1|delete".encode("utf-8")
                    ).hexdigest()[:32]
                    if self.store.insert_outbox(
                        outbox_id=outbox_id, memory_id=row["memory_id"],
                        workspace_id=item.workspace_id, target_type=target_type,
                        target_name=target_name, operation="delete", item_version=new_version,
                        index_schema_version="memory-index-v1",
                        embedding_model=None, embedding_dim=None, connection=connection,
                    ):
                        outbox_ids.append(outbox_id)
            result = MemoryDeleteResult(
                deleted_count=len(memory_ids), memory_ids=memory_ids, outbox_ids=outbox_ids,
            )
            self.store.insert_idempotency(
                workspace_id=item.workspace_id, principal_id=token.principal_id,
                idempotency_key=item.idempotency_key, operation_type="delete",
                request_sha256=request_hash, operation_id=item.operation_id,
                result=result.model_dump(mode="json"), status="completed", connection=connection,
            )
            return result
