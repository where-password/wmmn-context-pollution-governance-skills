from __future__ import annotations

import hashlib
import json
from datetime import datetime

from .db import SQLiteStore, utc_now_ts
from .models import AgentContextToken, ConsolidationInput, ConsolidationResult
from .security import TokenValidator


class ConsolidationService:
    """Idempotent consolidation coordinator.

    The local core does not invent evidence. Promotion to active canonical
    memory remains an evidence-producing DomainProfile workflow.
    """

    def __init__(self, store: SQLiteStore, token_validator: TokenValidator) -> None:
        self.store = store
        self.tokens = token_validator

    @staticmethod
    def _request_hash(item: ConsolidationInput) -> str:
        return hashlib.sha256(
            json.dumps(item.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    def consolidate(
        self, item: ConsolidationInput, *, token: AgentContextToken | None = None,
        scheduler_authorized: bool = False, now: datetime | None = None,
    ) -> ConsolidationResult:
        principal_id = "scheduler"
        if token is not None:
            context = self.tokens.validate(token, action="write", consume_query=False, now=now)
            if token.workspace_id != item.workspace_id or context.grant.workspace_id != item.workspace_id:
                raise PermissionError("consolidation workspace mismatch")
            principal_id = token.principal_id
        elif not scheduler_authorized:
            raise PermissionError("consolidation requires a token or scheduler authorization")

        request_hash = self._request_hash(item)
        existing = self.store.get_idempotency(item.workspace_id, principal_id, item.idempotency_key)
        if existing:
            if existing["request_sha256"] != request_hash:
                raise ValueError("idempotency key reused with a different consolidation request")
            return ConsolidationResult.model_validate_json(existing["result_json"])

        now_ts = utc_now_ts()
        with self.store.transaction() as connection:
            expired = connection.execute(
                "DELETE FROM working_memory WHERE task_id=? AND ttl_expires_at<?",
                (item.task_id, now_ts),
            ).rowcount
            rows = [dict(row) for row in connection.execute(
                "SELECT * FROM working_memory WHERE workspace_id=? AND project_id=? AND task_id=?",
                (item.workspace_id, item.project_id, item.task_id),
            )]
            skipped_secret = [row["wm_id"] for row in rows if row["sensitivity"] == "secret"]
            result = ConsolidationResult(
                task_id=item.task_id, promoted_memory_ids=[], conflicted_memory_ids=[],
                skipped_secret_wm_ids=skipped_secret, expired_wm_count=expired,
            )
            self.store.insert_idempotency(
                workspace_id=item.workspace_id, principal_id=principal_id,
                idempotency_key=item.idempotency_key, operation_type="consolidation",
                request_sha256=request_hash, operation_id=item.operation_id,
                result=result.model_dump(mode="json"), status="completed", connection=connection,
            )
            return result
