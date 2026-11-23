from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from conversation_cleaner.approval_execution import (  # noqa: E402
    ApprovalExecutionError,
    build_approval_execution_plan,
)
from conversation_cleaner.handoff_approval import create_handoff_approval  # noqa: E402
from conversation_cleaner.handoff_contracts import MemoryWriteHandoffItem  # noqa: E402
from conversation_cleaner.handoff_review import (  # noqa: E402
    APPROVAL_LEDGER_FILENAME,
    APPROVAL_MANIFEST_FILENAME,
    APPROVED_BATCH_FILENAME,
    generate_approved_batch,
    record_handoff_decision,
)
from conversation_cleaner.util import sha256_bytes, sha256_text  # noqa: E402


HANDOFF_ID = "handoff_444444444444444444444444"
ITEM_KEY = "mwh_execution_1"
REVIEWED_AT = "2026-11-22T12:00:00+08:00"


def _item(key: str = ITEM_KEY, content: str = "Execution memory.") -> MemoryWriteHandoffItem:
    return MemoryWriteHandoffItem.from_dict(
        {
            "idempotency_key": key,
            "operation_id": "op_" + key,
            "workspace_id": "ws_execution",
            "project_id": "project_execution",
            "task_id": None,
            "conversation_id": "conv_execution",
            "domain_profile": "generic",
            "profile_version": "1.0.0",
            "memory_type": "fact",
            "subtype": "fact",
            "subject": "execution",
            "entity_type": "topic",
            "entity_id": "topic-execution",
            "content": content,
            "importance": "medium",
            "declared_sensitivity": "private",
            "requested_visibility": "private",
            "assertion_source": "user_report",
            "verification_state": "quote_verified",
            "effective_from": None,
            "effective_to": None,
            "source_id": "src_execution",
            "source_unit_id": "su_execution",
            "source_scope": {
                "scope_type": "conversation_message",
                "ref": "msg_execution",
                "start": 0,
                "end": len(content),
                "locator_json": {"message_id": "msg_execution"},
            },
            "confidence": 0.9,
            "supersedes_ref": None,
            "supersedes_id": None,
            "expected_version": None,
            "domain_payload": {"source_sequence": 0},
            "evidences": [
                {
                    "evidence_id": "ev_" + key,
                    "source_unit_id": "su_execution",
                    "quote": content,
                    "locator_json": {"message_id": "msg_execution", "char_start": 0, "char_end": len(content)},
                    "status": "verified",
                }
            ],
            "claim_key": "claim:" + key,
            "scope_key": "workspace:ws_execution|project:project_execution|subject:execution",
            "observed_at": "2026-11-22T10:00:00+08:00",
            "transition_kind": "new",
        }
    )


def _write_handoff(root: Path, items: list[MemoryWriteHandoffItem]) -> Path:
    handoff_dir = root / "handoff"
    handoff_dir.mkdir()
    files = {
        "memory_write_batch.jsonl": "".join(
            json.dumps(item.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n" for item in items
        ),
        "source_units.jsonl": "",
        "state_transitions.jsonl": "",
        "review_pending.jsonl": "",
        "HANDOFF_REVIEW.md": "# Handoff review\\n",
    }
    for name, text in files.items():
        (handoff_dir / name).write_text(text, encoding="utf-8")
    manifest = {
        "handoff_id": HANDOFF_ID,
        "artifacts": {name: sha256_text(text) for name, text in files.items()},
    }
    (handoff_dir / "handoff_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return handoff_dir


def _record(handoff_dir: Path, item: MemoryWriteHandoffItem, decision: str, reviewed_at: str = REVIEWED_AT) -> None:
    record_handoff_decision(
        handoff_dir=handoff_dir,
        handoff_id=HANDOFF_ID,
        item=item,
        decision=decision,
        reviewer_id="reviewer-1",
        reason=f"Execution review {decision}.",
        reviewed_at=reviewed_at,
    )


def _write_approval_manifest_from_current(handoff_dir: Path) -> None:
    manifest = json.loads((handoff_dir / APPROVAL_MANIFEST_FILENAME).read_text(encoding="utf-8"))
    manifest["ledger_sha256"] = sha256_bytes((handoff_dir / APPROVAL_LEDGER_FILENAME).read_bytes())
    manifest["approved_batch_sha256"] = sha256_bytes((handoff_dir / APPROVED_BATCH_FILENAME).read_bytes())
    (handoff_dir / APPROVAL_MANIFEST_FILENAME).write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )


class ApprovalExecutionTests(unittest.TestCase):
    def test_unapproved_and_revoked_items_do_not_enter_writes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            pending = _item("mwh_execution_pending", "Pending item.")
            revoked = _item("mwh_execution_revoked", "Revoked item.")
            handoff_dir = _write_handoff(root, [pending, revoked])
            _record(handoff_dir, revoked, "revoke")
            generate_approved_batch(handoff_dir=handoff_dir)
            plan = build_approval_execution_plan(handoff_dir=handoff_dir, handoff_id=HANDOFF_ID, written_memory_map={})
            self.assertEqual(plan["approved_write_items"], [])
            self.assertEqual(plan["counts"]["blocked_items"], 2)
            self.assertEqual(plan["counts"]["pending_post_write_revocations"], 0)
            self.assertEqual([item["decision"] for item in plan["blocked_items"]], ["pending", "revoke"])
            self.assertEqual(plan["blocked_items"][1]["reason"], "revoked_before_write")

    def test_tampered_approved_batch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            item = _item()
            handoff_dir = _write_handoff(root, [item])
            _record(handoff_dir, item, "approve")
            generate_approved_batch(handoff_dir=handoff_dir)
            batch_path = handoff_dir / APPROVED_BATCH_FILENAME
            batch_path.write_text(batch_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ApprovalExecutionError, "approved_batch_sha256 does not match"):
                build_approval_execution_plan(handoff_dir=handoff_dir, handoff_id=HANDOFF_ID, written_memory_map={})

    def test_revoke_with_written_mapping_generates_delete_input(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            item = _item()
            handoff_dir = _write_handoff(root, [item])
            _record(handoff_dir, item, "approve", "2026-11-22T12:00:00+08:00")
            _record(handoff_dir, item, "revoke", "2026-11-22T12:05:00+08:00")
            generate_approved_batch(handoff_dir=handoff_dir)
            plan = build_approval_execution_plan(
                handoff_dir=handoff_dir,
                handoff_id=HANDOFF_ID,
                written_memory_map={ITEM_KEY: {"memory_id": "mem_execution_1", "version": 1}},
            )
            self.assertEqual(plan["approved_write_items"], [])
            self.assertEqual(len(plan["post_write_revocations"]), 1)
            delete_input = plan["post_write_revocations"][0]["delete_input"]
            self.assertEqual(delete_input["target_type"], "memory_id")
            self.assertEqual(delete_input["target_id"], "mem_execution_1")
            self.assertEqual(delete_input["expected_version"], 1)
            self.assertEqual(delete_input["delete_reason"], "Execution review revoke.")
            self.assertTrue(delete_input["idempotency_key"].startswith("mdel_"))
            self.assertTrue(delete_input["operation_id"].startswith("op_del_"))

    def test_revoke_without_written_mapping_is_pending(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            item = _item()
            handoff_dir = _write_handoff(root, [item])
            _record(handoff_dir, item, "approve", "2026-11-22T12:00:00+08:00")
            _record(handoff_dir, item, "revoke", "2026-11-22T12:05:00+08:00")
            generate_approved_batch(handoff_dir=handoff_dir)
            plan = build_approval_execution_plan(handoff_dir=handoff_dir, handoff_id=HANDOFF_ID, written_memory_map={})
            self.assertEqual(plan["post_write_revocations"], [])
            self.assertEqual(len(plan["pending_post_write_revocations"]), 1)
            self.assertEqual(plan["pending_post_write_revocations"][0]["idempotency_key"], ITEM_KEY)

    def test_latest_approve_after_write_is_already_applied(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            item = _item()
            handoff_dir = _write_handoff(root, [item])
            _record(handoff_dir, item, "approve")
            generate_approved_batch(handoff_dir=handoff_dir)
            plan = build_approval_execution_plan(
                handoff_dir=handoff_dir,
                handoff_id=HANDOFF_ID,
                written_memory_map={ITEM_KEY: {"memory_id": "mem_execution_1", "version": 1}},
            )
            self.assertEqual(plan["approved_write_items"], [])
            self.assertEqual(len(plan["already_applied_items"]), 1)
            self.assertEqual(plan["already_applied_items"][0]["memory_id"], "mem_execution_1")

    def test_revoke_then_later_approve_allows_a_new_write_plan(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            item = _item()
            handoff_dir = _write_handoff(root, [item])
            _record(handoff_dir, item, "approve", "2026-11-22T12:00:00+08:00")
            _record(handoff_dir, item, "revoke", "2026-11-22T12:05:00+08:00")
            _record(handoff_dir, item, "approve", "2026-11-22T12:10:00+08:00")
            generate_approved_batch(handoff_dir=handoff_dir)
            plan = build_approval_execution_plan(handoff_dir=handoff_dir, handoff_id=HANDOFF_ID, written_memory_map={})
            self.assertEqual(len(plan["approved_write_items"]), 1)
            self.assertEqual(plan["approved_write_items"][0]["item"]["idempotency_key"], ITEM_KEY)

    def test_cross_handoff_and_hash_tampering_are_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            item = _item()
            handoff_dir = _write_handoff(root, [item])
            _record(handoff_dir, item, "approve")
            generate_approved_batch(handoff_dir=handoff_dir)
            cross = create_handoff_approval(
                item=item,
                handoff_id="handoff_other",
                decision="approve",
                reviewer_id="reviewer-1",
                reason="Foreign handoff.",
                reviewed_at="2026-11-22T12:10:00+08:00",
            )
            ledger_path = handoff_dir / APPROVAL_LEDGER_FILENAME
            ledger_path.write_text(ledger_path.read_text(encoding="utf-8") + json.dumps(cross.to_dict(), sort_keys=True) + "\n", encoding="utf-8")
            _write_approval_manifest_from_current(handoff_dir)
            with self.assertRaisesRegex(ApprovalExecutionError, "different handoff"):
                build_approval_execution_plan(handoff_dir=handoff_dir, handoff_id=HANDOFF_ID, written_memory_map={})

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            item = _item()
            handoff_dir = _write_handoff(root, [item])
            _record(handoff_dir, item, "approve")
            generate_approved_batch(handoff_dir=handoff_dir)
            unknown_item = _item("mwh_unknown_execution", "Unknown execution key.")
            unknown_approval = create_handoff_approval(
                item=unknown_item,
                handoff_id=HANDOFF_ID,
                decision="approve",
                reviewer_id="reviewer-1",
                reason="Unknown key.",
                reviewed_at="2026-11-22T12:10:00+08:00",
            )
            ledger_path = handoff_dir / APPROVAL_LEDGER_FILENAME
            ledger_path.write_text(ledger_path.read_text(encoding="utf-8") + json.dumps(unknown_approval.to_dict(), sort_keys=True) + "\n", encoding="utf-8")
            _write_approval_manifest_from_current(handoff_dir)
            with self.assertRaisesRegex(ApprovalExecutionError, "unknown handoff item"):
                build_approval_execution_plan(handoff_dir=handoff_dir, handoff_id=HANDOFF_ID, written_memory_map={})

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            item = _item()
            handoff_dir = _write_handoff(root, [item])
            _record(handoff_dir, item, "approve")
            generate_approved_batch(handoff_dir=handoff_dir)
            batch_path = handoff_dir / "memory_write_batch.jsonl"
            changed = _item(content="Changed after handoff.")
            batch_path.write_text(json.dumps(changed.to_dict(), ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ApprovalExecutionError, "artifact hash does not match"):
                build_approval_execution_plan(handoff_dir=handoff_dir, handoff_id=HANDOFF_ID, written_memory_map={})

    def test_plan_is_deterministic_and_does_not_write(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            item = _item()
            handoff_dir = _write_handoff(root, [item])
            _record(handoff_dir, item, "approve")
            generate_approved_batch(handoff_dir=handoff_dir)
            before = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in handoff_dir.iterdir() if path.is_file()}
            first = build_approval_execution_plan(handoff_dir=handoff_dir, handoff_id=HANDOFF_ID, written_memory_map={})
            second = build_approval_execution_plan(handoff_dir=handoff_dir, handoff_id=HANDOFF_ID, written_memory_map={})
            self.assertEqual(first, second)
            self.assertFalse(first["production_writes"])
            after = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in handoff_dir.iterdir() if path.is_file()}
            self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
