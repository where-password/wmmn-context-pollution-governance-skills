from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from conversation_cleaner.handoff_approval import (  # noqa: E402
    APPROVAL_CONTRACT_VERSION,
    HandoffApproval,
    HandoffApprovalError,
    bind_approval_to_item,
    create_handoff_approval,
    handoff_item_sha256,
)
from conversation_cleaner.handoff_contracts import MemoryWriteHandoffItem  # noqa: E402


HANDOFF_ID = "handoff_approval_test"
REVIEWED_AT = "2026-11-22T12:00:00+08:00"


def _item_values(*, supersedes: bool = False, content: str = "Already upgraded to 32GB.") -> dict:
    return {
        "idempotency_key": "mwh_test_key",
        "operation_id": "op_test",
        "workspace_id": "ws_test",
        "project_id": "project_test",
        "task_id": None,
        "conversation_id": "conv_test",
        "domain_profile": "generic",
        "profile_version": "1.0.0",
        "memory_type": "fact",
        "subtype": "fact",
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
        "source_id": "src_test",
        "source_unit_id": "su_new",
        "source_scope": {
            "scope_type": "conversation_message",
            "ref": "msg_new",
            "start": 0,
            "end": len(content),
            "locator_json": {"message_id": "msg_new"},
        },
        "confidence": 0.95,
        "supersedes_ref": "obs_old" if supersedes else None,
        "supersedes_id": None,
        "expected_version": None,
        "domain_payload": {"existing": "kept"},
        "evidences": [
            {
                "evidence_id": "ev_new",
                "source_unit_id": "su_new",
                "quote": content,
                "locator_json": {"message_id": "msg_new", "char_start": 0, "char_end": len(content)},
                "status": "verified",
            }
        ],
        "claim_key": "claim:memory-upgrade",
        "scope_key": "workspace:ws_test|project:project_test|subject:device-memory",
        "observed_at": "2026-11-22T10:00:00+08:00",
        "transition_kind": "supersedes" if supersedes else "new",
    }


def _item(*, supersedes: bool = False, content: str = "Already upgraded to 32GB.") -> MemoryWriteHandoffItem:
    return MemoryWriteHandoffItem.from_dict(_item_values(supersedes=supersedes, content=content))


def _approval(
    item: MemoryWriteHandoffItem,
    *,
    handoff_id: str = HANDOFF_ID,
    decision: str = "approve",
    reviewer_id: str = "reviewer-1",
    reason: str = "Evidence and scope checked.",
    reviewed_at: str = REVIEWED_AT,
) -> HandoffApproval:
    return create_handoff_approval(
        item=item,
        handoff_id=handoff_id,
        decision=decision,
        reviewer_id=reviewer_id,
        reason=reason,
        reviewed_at=reviewed_at,
    )


class HandoffApprovalTests(unittest.TestCase):
    def test_approval_id_is_stable_and_serialization_roundtrips(self) -> None:
        item = _item()
        first = _approval(item)
        second = _approval(item)
        self.assertEqual(first.approval_id, second.approval_id)
        self.assertEqual(first.item_idempotency_key, item.idempotency_key)
        self.assertEqual(first.item_sha256, handoff_item_sha256(item))
        self.assertEqual(first.to_dict()["approval_contract_version"], APPROVAL_CONTRACT_VERSION)
        self.assertEqual(HandoffApproval.from_dict(first.to_dict()), first)

    def test_item_hash_tracks_content_evidence_and_transition_changes(self) -> None:
        base = _item()
        changed_content = _item(content="Already upgraded to 64GB.")
        changed_evidence = _item()
        changed_values = changed_evidence.to_dict()
        changed_values["evidences"][0]["status"] = "unresolved"
        changed_evidence = MemoryWriteHandoffItem.from_dict(changed_values)
        changed_transition = _item(supersedes=True)
        hashes = {
            handoff_item_sha256(base),
            handoff_item_sha256(changed_content),
            handoff_item_sha256(changed_evidence),
            handoff_item_sha256(changed_transition),
        }
        self.assertEqual(len(hashes), 4)

    def test_approval_id_changes_with_each_bound_field(self) -> None:
        item = _item()
        baseline = _approval(item)
        variants = {
            _approval(item, handoff_id="handoff_other").approval_id,
            _approval(_item(content="Changed content.")).approval_id,
            _approval(item, decision="reject").approval_id,
            _approval(item, reviewer_id="reviewer-2").approval_id,
            _approval(item, reason="A different review reason.").approval_id,
            _approval(item, reviewed_at="2026-11-22T13:00:00+08:00").approval_id,
        }
        self.assertNotIn(baseline.approval_id, variants)
        self.assertEqual(len(variants), 6)

    def test_strict_validation_rejects_unknown_fields_and_bad_values(self) -> None:
        item = _item()
        valid = _approval(item).to_dict()

        unknown = dict(valid)
        unknown["extra"] = True
        with self.assertRaisesRegex(HandoffApprovalError, "unknown fields"):
            HandoffApproval.from_dict(unknown)

        missing = dict(valid)
        del missing["reason"]
        with self.assertRaisesRegex(HandoffApprovalError, "missing required fields"):
            HandoffApproval.from_dict(missing)

        missing_version = dict(valid)
        del missing_version["approval_contract_version"]
        with self.assertRaisesRegex(HandoffApprovalError, "missing required fields"):
            HandoffApproval.from_dict(missing_version)

        wrong_version = dict(valid)
        wrong_version["approval_contract_version"] = "conversation-memory-handoff-approval-v0"
        with self.assertRaisesRegex(HandoffApprovalError, "approval_contract_version must be"):
            HandoffApproval.from_dict(wrong_version)

        tampered = dict(valid)
        tampered["reason"] = "Tampered after approval."
        with self.assertRaisesRegex(HandoffApprovalError, "approval_id does not match"):
            HandoffApproval.from_dict(tampered)

        with self.assertRaisesRegex(HandoffApprovalError, "decision must be one of"):
            _approval(item, decision="accepted")
        with self.assertRaisesRegex(HandoffApprovalError, "timezone offset"):
            _approval(item, reviewed_at="2026-11-22T12:00:00")
        with self.assertRaisesRegex(HandoffApprovalError, "reviewer_id must be a non-empty string"):
            _approval(item, reviewer_id=" ")
        with self.assertRaisesRegex(HandoffApprovalError, "reason must be a non-empty string"):
            _approval(item, reason="")

    def test_binding_requires_approve_and_exact_handoff_key_and_hash(self) -> None:
        item = _item()
        approved = _approval(item)
        self.assertEqual(bind_approval_to_item(approved, item, handoff_id=HANDOFF_ID), item)

        with self.assertRaisesRegex(HandoffApprovalError, "handoff_id does not match"):
            bind_approval_to_item(approved, item, handoff_id="handoff_other")

        changed_key_values = copy.deepcopy(item.to_dict())
        changed_key_values["idempotency_key"] = "mwh_other"
        changed_key = MemoryWriteHandoffItem.from_dict(changed_key_values)
        with self.assertRaisesRegex(HandoffApprovalError, "item_idempotency_key does not match"):
            bind_approval_to_item(approved, changed_key, handoff_id=HANDOFF_ID)

        changed_item = _item(content="Already upgraded to 64GB.")
        with self.assertRaisesRegex(HandoffApprovalError, "item_sha256 does not match"):
            bind_approval_to_item(approved, changed_item, handoff_id=HANDOFF_ID)

        for decision in ("reject", "defer"):
            with self.subTest(decision=decision):
                blocked = _approval(item, decision=decision)
                with self.assertRaisesRegex(HandoffApprovalError, "only an approve decision"):
                    bind_approval_to_item(blocked, item, handoff_id=HANDOFF_ID)


if __name__ == "__main__":
    unittest.main()
