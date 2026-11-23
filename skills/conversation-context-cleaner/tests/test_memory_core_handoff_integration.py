from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from conversation_cleaner.database_handoff_adapter import to_memory_write_input  # noqa: E402
from conversation_cleaner.handoff_approval import create_handoff_approval  # noqa: E402
from conversation_cleaner.handoff_contracts import (  # noqa: E402
    HandoffEvidence,
    HandoffSourceScope,
    MemoryWriteHandoffItem,
)


try:
    from memory_core.db import SQLiteStore
    from memory_core.models import (
        AccessGrant,
        AccessScope,
        EvidenceStatus,
        MemoryStatus,
        MemoryWriteInput,
        SensitivityLevel,
    )
    from memory_core.profiles import ProfileRegistry
    from memory_core.security import TokenValidator, issue_token
    from memory_core.writer import MemoryWriter

    MEMORY_CORE_AVAILABLE = True
    MEMORY_CORE_IMPORT_ERROR = ""
except Exception as exc:  # pragma: no cover - only exercised by the standard-library runner
    MEMORY_CORE_AVAILABLE = False
    MEMORY_CORE_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"


WORKSPACE_ID = "ws_handoff_integration"
PROJECT_ID = "project_handoff_integration"
SOURCE_ID = "src_handoff_integration"
PRINCIPAL_ID = "handoff-reviewer"
SECRET = b"temporary-handoff-integration-secret"
CLAIM_KEY = "claim:memory-upgrade"
SCOPE_KEY = "workspace:ws_handoff_integration|project:project_handoff_integration|subject:device-memory"
HANDOFF_ID = "handoff_integration_test"
REVIEWED_AT = "2026-11-22T12:00:00+08:00"


def _evidence(evidence_id: str, source_unit_id: str, quote: str, status: str) -> HandoffEvidence:
    return HandoffEvidence.from_dict(
        {
            "evidence_id": evidence_id,
            "source_unit_id": source_unit_id,
            "quote": quote,
            "locator_json": {"message_id": evidence_id, "char_start": 0, "char_end": len(quote)},
            "status": status,
        }
    )


def _handoff_item(
    *,
    observation_id: str,
    content: str,
    source_sequence: int,
    source_unit_id: str,
    transition_kind: str,
    idempotency_key: str,
    operation_id: str,
    evidences: list[HandoffEvidence],
    supersedes_ref: str | None = None,
) -> MemoryWriteHandoffItem:
    return MemoryWriteHandoffItem.from_dict(
        {
            "idempotency_key": idempotency_key,
            "operation_id": operation_id,
            "workspace_id": WORKSPACE_ID,
            "project_id": PROJECT_ID,
            "task_id": None,
            "conversation_id": "conv_handoff_integration",
            "domain_profile": "generic",
            "profile_version": "1.0.0",
            "memory_type": "constraint" if transition_kind == "new" else "fact",
            "subtype": "constraint" if transition_kind == "new" else "fact",
            "subject": "device memory",
            "entity_type": "device",
            "entity_id": "device-1",
            "content": content,
            "importance": "high",
            "declared_sensitivity": "private",
            "requested_visibility": "private",
            "assertion_source": "user_report",
            "verification_state": "quote_verified",
            "effective_from": None,
            "effective_to": None,
            "source_id": SOURCE_ID,
            "source_unit_id": source_unit_id,
            "source_scope": HandoffSourceScope(
                scope_type="conversation_message",
                ref=observation_id,
                start=source_sequence,
                end=source_sequence,
                locator_json={"observation_id": observation_id},
            ).to_dict(),
            "confidence": 0.95,
            "supersedes_ref": supersedes_ref,
            "supersedes_id": None,
            "expected_version": None,
            "domain_payload": {
                "memory_observation_id": observation_id,
                "state_kind": "constraint" if transition_kind == "new" else "fact",
            },
            "evidences": [item.to_dict() for item in evidences],
            "claim_key": CLAIM_KEY,
            "scope_key": SCOPE_KEY,
            "observed_at": f"2026-11-22T10:0{source_sequence}:00+08:00",
            "transition_kind": transition_kind,
        }
    )


@unittest.skipUnless(
    MEMORY_CORE_AVAILABLE,
    "requires chat_etl_v1 venv and PYTHONPATH=E:\\污染治理---数据库\\memory_core_v1\\src",
)
class MemoryCoreHandoffIntegrationTests(unittest.TestCase):
    def test_adapter_supersede_version_and_idempotent_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = SQLiteStore(root / "memory.sqlite3")
            store.migrate()
            store.ensure_workspace_project(
                workspace_id=WORKSPACE_ID,
                workspace_name="Handoff integration workspace",
                project_id=PROJECT_ID,
                project_name="Handoff integration project",
            )
            profiles = ProfileRegistry()
            store.register_profiles(profiles)

            old_text = "暂时不升级内存，因为没有预算。"
            new_text = "已经升级到 32GB 内存。"
            store.insert_source(
                source_id=SOURCE_ID,
                workspace_id=WORKSPACE_ID,
                project_id=PROJECT_ID,
                visibility="workspace",
                owner_principal_id=None,
                source_path="memory://handoff-integration",
                source_sha256=hashlib.sha256((old_text + new_text).encode("utf-8")).hexdigest(),
                source_type="conversation",
                sensitivity=SensitivityLevel.PRIVATE.value,
            )
            for unit_id, text, sequence in (
                ("su_old", old_text, 0),
                ("su_new", new_text, 2),
            ):
                store.insert_source_unit(
                    source_unit_id=unit_id,
                    source_id=SOURCE_ID,
                    parent_unit_id=None,
                    scope_type="conversation_message",
                    scope_ref=f"message-{sequence}",
                    source_unit_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    text_content=text,
                    metadata={"sequence": sequence},
                )

            now = datetime.now(timezone.utc)
            grant = AccessGrant(
                grant_id="grant-handoff-integration",
                principal_id=PRINCIPAL_ID,
                workspace_id=WORKSPACE_ID,
                scope=AccessScope(
                    project_ids=[PROJECT_ID],
                    domains=["generic"],
                    max_sensitivity=SensitivityLevel.PRIVATE,
                    allow_global_kb=False,
                ),
                actions=["write", "search"],
                expires_at=now + timedelta(hours=1),
                remaining_queries=None,
                purpose="isolated handoff integration",
                created_at=now,
            )
            store.save_grant(grant)
            token_validator = TokenValidator(store, secret=SECRET)
            writer = MemoryWriter(store=store, profiles=profiles, token_validator=token_validator)

            def fresh_token():
                return issue_token(
                    secret=SECRET,
                    principal_id=PRINCIPAL_ID,
                    task_id="handoff-integration-task",
                    grant_id=grant.grant_id,
                    workspace_id=WORKSPACE_ID,
                    ttl_seconds=300,
                )

            old_item = _handoff_item(
                observation_id="obs_memory_not_upgraded",
                content=old_text,
                source_sequence=0,
                source_unit_id="su_old",
                transition_kind="new",
                idempotency_key="mwh_integration_old",
                operation_id="op_integration_old",
                evidences=[_evidence("ev_old", "su_old", old_text, "verified")],
            )
            old_approval = create_handoff_approval(
                item=old_item,
                handoff_id=HANDOFF_ID,
                decision="approve",
                reviewer_id=PRINCIPAL_ID,
                reason="Approved isolated old-state integration handoff.",
                reviewed_at=REVIEWED_AT,
            )
            old_payload = to_memory_write_input(
                old_item,
                handoff_id=HANDOFF_ID,
                approval=old_approval,
            )
            old_input = MemoryWriteInput.model_validate(old_payload)
            old_result = writer.write(old_input, fresh_token())
            self.assertEqual(old_result.version, 1)
            self.assertEqual(old_result.status, MemoryStatus.ACTIVE)

            new_item = _handoff_item(
                observation_id="obs_memory_upgraded_32gb",
                content=new_text,
                source_sequence=2,
                source_unit_id="su_new",
                transition_kind="supersedes",
                idempotency_key="mwh_integration_new",
                operation_id="op_integration_new",
                evidences=[
                    _evidence("ev_old", "su_old", old_text, "unresolved"),
                    _evidence("ev_new", "su_new", new_text, "unresolved"),
                ],
                supersedes_ref="obs_memory_not_upgraded",
            )
            new_approval = create_handoff_approval(
                item=new_item,
                handoff_id=HANDOFF_ID,
                decision="approve",
                reviewer_id=PRINCIPAL_ID,
                reason="Approved isolated superseding integration handoff.",
                reviewed_at=REVIEWED_AT,
            )
            new_payload = to_memory_write_input(
                new_item,
                handoff_id=HANDOFF_ID,
                approval=new_approval,
                supersedes_ref_to_id={"obs_memory_not_upgraded": old_result.memory_id},
                expected_version=1,
            )
            new_input = MemoryWriteInput.model_validate(new_payload)
            self.assertEqual(new_input.supersedes_id, old_result.memory_id)
            self.assertEqual(new_input.expected_version, 1)
            self.assertEqual(new_payload["domain_payload"]["conversation_handoff"]["claim_key"], CLAIM_KEY)
            self.assertNotIn("claim_key", new_payload)

            new_result = writer.write(new_input, fresh_token())
            self.assertEqual(new_result.version, 2)
            self.assertEqual(new_result.status, MemoryStatus.PENDING_REVIEW)
            self.assertEqual(new_result.supersedes_id if hasattr(new_result, "supersedes_id") else new_input.supersedes_id, old_result.memory_id)

            old_record = store.get_memory_record(old_result.memory_id)
            new_record = store.get_memory_record(new_result.memory_id)
            self.assertEqual(old_record.status, MemoryStatus.SUPERSEDED)
            self.assertEqual(new_record.status, MemoryStatus.PENDING_REVIEW)
            self.assertEqual(new_record.version, 2)
            self.assertEqual(new_record.supersedes_id, old_result.memory_id)
            self.assertEqual(new_record.domain_payload["conversation_handoff"]["transition_kind"], "supersedes")
            self.assertEqual({item.quote for item in new_record.evidences}, {old_text, new_text})

            repeated = writer.write(new_input, fresh_token())
            self.assertEqual(repeated.memory_id, new_result.memory_id)
            self.assertEqual(repeated.version, 2)
            with store.read() as connection:
                count = connection.execute(
                    "SELECT COUNT(*) AS count FROM memory_items WHERE workspace_id=? AND project_id=?",
                    (WORKSPACE_ID, PROJECT_ID),
                ).fetchone()["count"]
            self.assertEqual(count, 2)
            self.assertEqual(store.get_memory_record(old_result.memory_id).status, MemoryStatus.SUPERSEDED)
            self.assertEqual(store.get_memory_record(new_result.memory_id).status, MemoryStatus.PENDING_REVIEW)


if __name__ == "__main__":
    unittest.main()
