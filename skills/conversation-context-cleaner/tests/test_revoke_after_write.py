from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from conversation_cleaner.approval_execution import build_approval_execution_plan  # noqa: E402
from conversation_cleaner.database_handoff_adapter import to_memory_write_input  # noqa: E402
from conversation_cleaner.handoff_approval import HandoffApproval  # noqa: E402
from conversation_cleaner.handoff_contracts import MemoryWriteHandoffItem  # noqa: E402
from conversation_cleaner.handoff_review import generate_approved_batch, record_handoff_decision  # noqa: E402
from conversation_cleaner.util import sha256_text  # noqa: E402


try:
    from memory_core.db import SQLiteStore
    from memory_core.delete import MemoryDeleteService
    from memory_core.models import (
        AccessGrant,
        AccessScope,
        EvidenceStatus,
        MemoryDeleteInput,
        MemoryStatus,
        MemoryWriteInput,
        SensitivityLevel,
    )
    from memory_core.profiles import ProfileRegistry
    from memory_core.security import TokenValidator, issue_token
    from memory_core.writer import MemoryWriter

    MEMORY_CORE_AVAILABLE = True
except Exception:
    MEMORY_CORE_AVAILABLE = False


HANDOFF_ID = "handoff_555555555555555555555555"
ITEM_KEY = "mwh_revoke_after_write"
WORKSPACE_ID = "ws_revoke_after_write"
PROJECT_ID = "project_revoke_after_write"
SOURCE_ID = "src_revoke_after_write"
PRINCIPAL_ID = "revoke-reviewer"
SECRET = b"temporary-revoke-after-write-secret"
CONTENT = "Post-write revocation memory."


def _item() -> MemoryWriteHandoffItem:
    return MemoryWriteHandoffItem.from_dict(
        {
            "idempotency_key": ITEM_KEY,
            "operation_id": "op_revoke_after_write",
            "workspace_id": WORKSPACE_ID,
            "project_id": PROJECT_ID,
            "task_id": None,
            "conversation_id": "conv_revoke_after_write",
            "domain_profile": "generic",
            "profile_version": "1.0.0",
            "memory_type": "fact",
            "subtype": "fact",
            "subject": "revoke after write",
            "entity_type": "topic",
            "entity_id": "topic-revoke",
            "content": CONTENT,
            "importance": "medium",
            "declared_sensitivity": "private",
            "requested_visibility": "private",
            "assertion_source": "user_report",
            "verification_state": "quote_verified",
            "effective_from": None,
            "effective_to": None,
            "source_id": SOURCE_ID,
            "source_unit_id": "su_revoke_after_write",
            "source_scope": {
                "scope_type": "conversation_message",
                "ref": "msg_revoke_after_write",
                "start": 0,
                "end": len(CONTENT),
                "locator_json": {"message_id": "msg_revoke_after_write"},
            },
            "confidence": 0.95,
            "supersedes_ref": None,
            "supersedes_id": None,
            "expected_version": None,
            "domain_payload": {"source_sequence": 0},
            "evidences": [
                {
                    "evidence_id": "ev_revoke_after_write",
                    "source_unit_id": "su_revoke_after_write",
                    "quote": CONTENT,
                    "locator_json": {"message_id": "msg_revoke_after_write", "char_start": 0, "char_end": len(CONTENT)},
                    "status": "verified",
                }
            ],
            "claim_key": "claim:revoke-after-write",
            "scope_key": "workspace:ws_revoke_after_write|project:project_revoke_after_write|subject:revoke-after-write",
            "observed_at": "2026-11-22T10:00:00+08:00",
            "transition_kind": "new",
        }
    )


def _write_handoff(root: Path) -> Path:
    handoff_dir = root / "handoff"
    handoff_dir.mkdir()
    files = {
        "memory_write_batch.jsonl": json.dumps(_item().to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        "source_units.jsonl": "",
        "state_transitions.jsonl": "",
        "review_pending.jsonl": "",
        "HANDOFF_REVIEW.md": "# Handoff review\n",
    }
    for name, text in files.items():
        (handoff_dir / name).write_text(text, encoding="utf-8", newline="\n")
    manifest = {
        "handoff_id": HANDOFF_ID,
        "source_sha256": hashlib.sha256(CONTENT.encode("utf-8")).hexdigest(),
        "artifacts": {name: sha256_text(text) for name, text in files.items()},
    }
    (handoff_dir / "handoff_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return handoff_dir


def _record(handoff_dir: Path, decision: str, reviewed_at: str) -> None:
    record_handoff_decision(
        handoff_dir=handoff_dir,
        handoff_id=HANDOFF_ID,
        item=_item(),
        decision=decision,
        reviewer_id=PRINCIPAL_ID,
        reason=f"Post-write lifecycle decision: {decision}.",
        reviewed_at=reviewed_at,
    )


@unittest.skipUnless(
    MEMORY_CORE_AVAILABLE,
    "requires chat_etl_v1 venv and PYTHONPATH=E:\\污染治理---数据库\\memory_core_v1\\src",
)
class RevokeAfterWriteTests(unittest.TestCase):
    def test_revoke_after_write_creates_tombstone_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            handoff_dir = _write_handoff(root)
            _record(handoff_dir, "approve", "2026-11-22T12:00:00+08:00")
            generate_approved_batch(handoff_dir=handoff_dir)

            pre_write_plan = build_approval_execution_plan(
                handoff_dir=handoff_dir,
                handoff_id=HANDOFF_ID,
                written_memory_map={},
            )
            self.assertEqual(pre_write_plan["counts"]["approved_write_items"], 1)
            approved = pre_write_plan["approved_write_items"][0]
            approved_item = MemoryWriteHandoffItem.from_dict(approved["item"])
            approval = HandoffApproval.from_dict(approved["approval"])

            store = SQLiteStore(root / "memory.sqlite3")
            store.migrate()
            store.ensure_workspace_project(
                workspace_id=WORKSPACE_ID,
                workspace_name="Revoke after write workspace",
                project_id=PROJECT_ID,
                project_name="Revoke after write project",
            )
            profiles = ProfileRegistry()
            store.register_profiles(profiles)
            store.insert_source(
                source_id=SOURCE_ID,
                workspace_id=WORKSPACE_ID,
                project_id=PROJECT_ID,
                visibility="workspace",
                owner_principal_id=None,
                source_path="memory://revoke-after-write",
                source_sha256=hashlib.sha256(CONTENT.encode("utf-8")).hexdigest(),
                source_type="conversation",
                sensitivity=SensitivityLevel.PRIVATE.value,
            )
            store.insert_source_unit(
                source_unit_id="su_revoke_after_write",
                source_id=SOURCE_ID,
                parent_unit_id=None,
                scope_type="conversation_message",
                scope_ref="msg_revoke_after_write",
                source_unit_hash=hashlib.sha256(CONTENT.encode("utf-8")).hexdigest(),
                text_content=CONTENT,
                metadata={"sequence": 0},
            )
            now = datetime.now(timezone.utc)
            grant = AccessGrant(
                grant_id="grant-revoke-after-write",
                principal_id=PRINCIPAL_ID,
                workspace_id=WORKSPACE_ID,
                scope=AccessScope(
                    project_ids=[PROJECT_ID],
                    domains=["generic"],
                    max_sensitivity=SensitivityLevel.PRIVATE,
                    allow_global_kb=False,
                ),
                actions=["write", "search", "delete"],
                expires_at=now + timedelta(hours=1),
                remaining_queries=None,
                purpose="isolated revoke-after-write test",
                created_at=now,
            )
            store.save_grant(grant)
            validator = TokenValidator(store, secret=SECRET)
            writer = MemoryWriter(store=store, profiles=profiles, token_validator=validator)
            delete_service = MemoryDeleteService(store=store, token_validator=validator)

            def fresh_token():
                return issue_token(
                    secret=SECRET,
                    principal_id=PRINCIPAL_ID,
                    task_id="revoke-after-write",
                    grant_id=grant.grant_id,
                    workspace_id=WORKSPACE_ID,
                    ttl_seconds=300,
                )

            write_payload = to_memory_write_input(
                approved_item,
                handoff_id=HANDOFF_ID,
                approval=approval,
            )
            write_input = MemoryWriteInput.model_validate(write_payload)
            self.assertTrue(all(evidence.status == EvidenceStatus.VERIFIED for evidence in write_input.evidences))
            written = writer.write(write_input, fresh_token())
            self.assertEqual(written.version, 1)
            self.assertEqual(written.status, MemoryStatus.ACTIVE)

            _record(handoff_dir, "revoke", "2026-11-22T12:05:00+08:00")
            generate_approved_batch(handoff_dir=handoff_dir)
            post_write_plan = build_approval_execution_plan(
                handoff_dir=handoff_dir,
                handoff_id=HANDOFF_ID,
                written_memory_map={ITEM_KEY: {"memory_id": written.memory_id, "version": written.version}},
            )
            self.assertEqual(post_write_plan["counts"]["post_write_revocations"], 1)
            delete_input = MemoryDeleteInput.model_validate(
                post_write_plan["post_write_revocations"][0]["delete_input"]
            )
            deleted = delete_service.delete(delete_input, fresh_token())
            self.assertEqual(deleted.deleted_count, 1)
            self.assertEqual(deleted.memory_ids, [written.memory_id])

            record = store.get_memory_record(written.memory_id)
            self.assertIsNotNone(record)
            self.assertIsNotNone(record.deleted_at)
            self.assertEqual(record.delete_reason, "Post-write lifecycle decision: revoke.")
            self.assertEqual(record.version, 2)
            with store.read() as connection:
                count = connection.execute(
                    "SELECT COUNT(*) AS count FROM memory_items WHERE workspace_id=? AND project_id=?",
                    (WORKSPACE_ID, PROJECT_ID),
                ).fetchone()["count"]
            self.assertEqual(count, 1)

            repeated = delete_service.delete(delete_input, fresh_token())
            self.assertEqual(repeated, deleted)
            self.assertEqual(store.get_memory_record(written.memory_id).version, 2)
            with store.read() as connection:
                count = connection.execute(
                    "SELECT COUNT(*) AS count FROM memory_items WHERE workspace_id=? AND project_id=?",
                    (WORKSPACE_ID, PROJECT_ID),
                ).fetchone()["count"]
            self.assertEqual(count, 1)

            approval_manifest = json.loads((handoff_dir / "approval_manifest.json").read_text(encoding="utf-8"))
            for field in ("production_writes", "real_llm_called", "milvus_written", "alias_changed"):
                self.assertIs(approval_manifest[field], False)


if __name__ == "__main__":
    unittest.main()
