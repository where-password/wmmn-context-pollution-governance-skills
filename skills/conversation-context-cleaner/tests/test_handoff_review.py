from __future__ import annotations

import ast
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

from conversation_cleaner.handoff_approval import (  # noqa: E402
    APPROVAL_CONTRACT_VERSION,
    create_handoff_approval,
)
from conversation_cleaner.handoff_contracts import MemoryWriteHandoffItem  # noqa: E402
from conversation_cleaner.handoff_review import (  # noqa: E402
    APPROVAL_LEDGER_FILENAME,
    APPROVAL_MANIFEST_FILENAME,
    APPROVAL_REVIEW_FILENAME,
    APPROVED_BATCH_FILENAME,
    HandoffReviewError,
    generate_approved_batch,
    read_approval_ledger,
    record_handoff_decision,
)


HANDOFF_ID = "handoff_review_test"
REVIEWED_AT = "2026-11-22T12:00:00+08:00"


def _item_values(key: str = "mwh_review_1", content: str = "Remember the approved memory.") -> dict:
    return {
        "idempotency_key": key,
        "operation_id": "op_" + key,
        "workspace_id": "ws_review",
        "project_id": "project_review",
        "task_id": None,
        "conversation_id": "conv_review",
        "domain_profile": "generic",
        "profile_version": "1.0.0",
        "memory_type": "fact",
        "subtype": "fact",
        "subject": "reviewed memory",
        "entity_type": "topic",
        "entity_id": "topic-1",
        "content": content,
        "importance": "medium",
        "declared_sensitivity": "private",
        "requested_visibility": "private",
        "assertion_source": "user_report",
        "verification_state": "quote_verified",
        "effective_from": None,
        "effective_to": None,
        "source_id": "src_review",
        "source_unit_id": "su_review",
        "source_scope": {
            "scope_type": "conversation_message",
            "ref": "msg_review",
            "start": 0,
            "end": len(content),
            "locator_json": {"message_id": "msg_review"},
        },
        "confidence": 0.9,
        "supersedes_ref": None,
        "supersedes_id": None,
        "expected_version": None,
        "domain_payload": {"source_sequence": int(key.rsplit("_", 1)[-1]) if key.rsplit("_", 1)[-1].isdigit() else 0},
        "evidences": [
            {
                "evidence_id": "ev_" + key,
                "source_unit_id": "su_review",
                "quote": content,
                "locator_json": {"message_id": "msg_review", "char_start": 0, "char_end": len(content)},
                "status": "verified",
            }
        ],
        "claim_key": "claim:" + key,
        "scope_key": "workspace:ws_review|project:project_review|subject:reviewed-memory",
        "observed_at": "2026-11-22T10:00:00+08:00",
        "transition_kind": "new",
    }


def _item(key: str = "mwh_review_1", content: str = "Remember the approved memory.") -> MemoryWriteHandoffItem:
    return MemoryWriteHandoffItem.from_dict(_item_values(key=key, content=content))


def _write_handoff(root: Path, items: list[MemoryWriteHandoffItem]) -> Path:
    handoff_dir = root / "handoff"
    handoff_dir.mkdir()
    (handoff_dir / "memory_write_batch.jsonl").write_text(
        "".join(json.dumps(item.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n" for item in items),
        encoding="utf-8",
    )
    (handoff_dir / "handoff_manifest.json").write_text(
        json.dumps({"handoff_id": HANDOFF_ID}, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return handoff_dir


def _record(
    handoff_dir: Path,
    item: MemoryWriteHandoffItem,
    decision: str,
    *,
    reviewed_at: str = REVIEWED_AT,
    reviewer_id: str = "reviewer-1",
    reason: str = "Controlled approval review.",
) -> dict:
    return record_handoff_decision(
        handoff_dir=handoff_dir,
        handoff_id=HANDOFF_ID,
        item=item,
        decision=decision,
        reviewer_id=reviewer_id,
        reason=reason,
        reviewed_at=reviewed_at,
    )


def _records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class HandoffReviewTests(unittest.TestCase):
    def test_approve_generates_batch_manifest_review_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            item = _item()
            handoff_dir = _write_handoff(root, [item])
            recorded = _record(handoff_dir, item, "approve")
            self.assertEqual(recorded["status"], "recorded")
            self.assertTrue(recorded["changed"])

            generated = generate_approved_batch(handoff_dir=handoff_dir, handoff_id=HANDOFF_ID)
            self.assertEqual(generated["status"], "generated")
            self.assertTrue(generated["changed"])
            self.assertEqual(generated["counts"], {"approved": 1, "rejected": 0, "deferred": 0, "revoked": 0, "pending": 0})

            batch_path = handoff_dir / APPROVED_BATCH_FILENAME
            manifest_path = handoff_dir / APPROVAL_MANIFEST_FILENAME
            review_path = handoff_dir / APPROVAL_REVIEW_FILENAME
            ledger_path = handoff_dir / APPROVAL_LEDGER_FILENAME
            ledger_records = _records(ledger_path)
            self.assertEqual(ledger_records[0]["approval_contract_version"], APPROVAL_CONTRACT_VERSION)
            self.assertEqual([record["idempotency_key"] for record in _records(batch_path)], [item.idempotency_key])

            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["handoff_id"], HANDOFF_ID)
            self.assertEqual(manifest["approved_count"], 1)
            self.assertEqual(manifest["rejected_count"], 0)
            self.assertEqual(manifest["deferred_count"], 0)
            self.assertEqual(manifest["revoked_count"], 0)
            self.assertEqual(manifest["pending_count"], 0)
            self.assertEqual(manifest["ledger_sha256"], hashlib.sha256(ledger_path.read_bytes()).hexdigest())
            self.assertEqual(manifest["approved_batch_sha256"], hashlib.sha256(batch_path.read_bytes()).hexdigest())
            for field in ("production_writes", "real_llm_called", "milvus_written", "alias_changed"):
                self.assertIs(manifest[field], False)
            review = review_path.read_text(encoding="utf-8")
            self.assertIn("reviewer-1", review)
            self.assertIn("Controlled approval review.", review)
            self.assertIn("matched", review)
            self.assertIn("No canonical database, Milvus, or alias write has occurred", review)

            before = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in (batch_path, manifest_path, review_path)}
            repeated = generate_approved_batch(handoff_dir=handoff_dir)
            self.assertEqual(repeated["status"], "already_generated")
            self.assertFalse(repeated["changed"])
            after = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in (batch_path, manifest_path, review_path)}
            self.assertEqual(before, after)
            self.assertEqual({path.name for path in root.iterdir()}, {"handoff"})

    def test_reject_and_defer_are_excluded_and_latest_decision_wins(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            rejected_item = _item("mwh_review_1", "Rejected item.")
            deferred_item = _item("mwh_review_2", "Deferred item.")
            pending_item = _item("mwh_review_3", "Pending item.")
            handoff_dir = _write_handoff(root, [rejected_item, deferred_item, pending_item])
            _record(handoff_dir, rejected_item, "approve", reviewed_at="2026-11-22T12:00:00+08:00")
            _record(handoff_dir, rejected_item, "reject", reviewed_at="2026-11-22T12:01:00+08:00")
            _record(handoff_dir, deferred_item, "defer", reviewed_at="2026-11-22T12:02:00+08:00")

            generated = generate_approved_batch(handoff_dir=handoff_dir)
            self.assertEqual(generated["counts"], {"approved": 0, "rejected": 1, "deferred": 1, "revoked": 0, "pending": 1})
            self.assertEqual(_records(handoff_dir / APPROVED_BATCH_FILENAME), [])
            ledger = read_approval_ledger(handoff_dir)
            self.assertEqual(len(ledger), 3)

    def test_revoke_overrides_earlier_approve(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            item = _item()
            handoff_dir = _write_handoff(root, [item])
            _record(handoff_dir, item, "approve", reviewed_at="2026-11-22T12:00:00+08:00")
            first = generate_approved_batch(handoff_dir=handoff_dir)
            self.assertEqual(first["counts"]["approved"], 1)

            _record(handoff_dir, item, "revoke", reviewed_at="2026-11-22T12:05:00+08:00")
            second = generate_approved_batch(handoff_dir=handoff_dir)
            self.assertEqual(second["counts"], {"approved": 0, "rejected": 0, "deferred": 0, "revoked": 1, "pending": 0})
            self.assertEqual(_records(handoff_dir / APPROVED_BATCH_FILENAME), [])
            self.assertIn("`revoke`", (handoff_dir / APPROVAL_REVIEW_FILENAME).read_text(encoding="utf-8"))

            _record(handoff_dir, item, "approve", reviewed_at="2026-11-22T12:10:00+08:00")
            third = generate_approved_batch(handoff_dir=handoff_dir)
            self.assertEqual(third["counts"]["approved"], 1)
            self.assertEqual(len(_records(handoff_dir / APPROVED_BATCH_FILENAME)), 1)

    def test_exact_duplicate_decision_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            item = _item()
            handoff_dir = _write_handoff(root, [item])
            first = _record(handoff_dir, item, "approve")
            second = _record(handoff_dir, item, "approve")
            self.assertEqual(first["status"], "recorded")
            self.assertEqual(second["status"], "duplicate")
            self.assertFalse(second["changed"])
            self.assertEqual(len(_records(handoff_dir / APPROVAL_LEDGER_FILENAME)), 1)

    def test_same_reviewed_at_with_different_decision_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            item = _item()
            handoff_dir = _write_handoff(root, [item])
            _record(handoff_dir, item, "approve", reviewed_at="2026-11-22T12:00:00+08:00")
            with self.assertRaisesRegex(HandoffReviewError, "conflicting decision at the same reviewed_at"):
                _record(handoff_dir, item, "reject", reviewed_at="2026-11-22T04:00:00Z")

    def test_cross_handoff_key_and_hash_records_are_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            item = _item()
            handoff_dir = _write_handoff(root, [item])
            cross_handoff = create_handoff_approval(
                item=item,
                handoff_id="handoff_other",
                decision="approve",
                reviewer_id="reviewer-1",
                reason="Wrong handoff.",
                reviewed_at=REVIEWED_AT,
            )
            (handoff_dir / APPROVAL_LEDGER_FILENAME).write_text(
                json.dumps(cross_handoff.to_dict(), ensure_ascii=False, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(HandoffReviewError, "different handoff"):
                generate_approved_batch(handoff_dir=handoff_dir)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            approved_item = _item("mwh_review_1")
            other_item = _item("mwh_review_2", "A different item.")
            handoff_dir = _write_handoff(root, [other_item])
            approval = create_handoff_approval(
                item=approved_item,
                handoff_id=HANDOFF_ID,
                decision="approve",
                reviewer_id="reviewer-1",
                reason="Unknown key.",
                reviewed_at=REVIEWED_AT,
            )
            (handoff_dir / APPROVAL_LEDGER_FILENAME).write_text(
                json.dumps(approval.to_dict(), ensure_ascii=False, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(HandoffReviewError, "unknown handoff item"):
                generate_approved_batch(handoff_dir=handoff_dir)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            item = _item()
            handoff_dir = _write_handoff(root, [item])
            _record(handoff_dir, item, "approve")
            changed = _item(content="Changed after approval.")
            (handoff_dir / "memory_write_batch.jsonl").write_text(
                json.dumps(changed.to_dict(), ensure_ascii=False, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(HandoffReviewError, "item_sha256 does not match"):
                generate_approved_batch(handoff_dir=handoff_dir)

    def test_damaged_and_unknown_ledger_records_are_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            item = _item()
            handoff_dir = _write_handoff(root, [item])
            (handoff_dir / APPROVAL_LEDGER_FILENAME).write_text("{not-json}\n", encoding="utf-8")
            with self.assertRaisesRegex(HandoffReviewError, "not valid JSON"):
                read_approval_ledger(handoff_dir)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            item = _item()
            handoff_dir = _write_handoff(root, [item])
            approval = create_handoff_approval(
                item=item,
                handoff_id=HANDOFF_ID,
                decision="approve",
                reviewer_id="reviewer-1",
                reason="Unknown field test.",
                reviewed_at=REVIEWED_AT,
            )
            payload = approval.to_dict()
            payload["unknown"] = True
            (handoff_dir / APPROVAL_LEDGER_FILENAME).write_text(
                json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(HandoffReviewError, "strict approval validation"):
                read_approval_ledger(handoff_dir)

    def test_module_has_no_network_or_database_imports(self) -> None:
        path = SCRIPTS / "conversation_cleaner" / "handoff_review.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        modules: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules.add(node.module.split(".", 1)[0])
        for forbidden in ("memory_core", "pydantic", "socket", "requests", "httpx"):
            self.assertNotIn(forbidden, modules)


if __name__ == "__main__":
    unittest.main()
