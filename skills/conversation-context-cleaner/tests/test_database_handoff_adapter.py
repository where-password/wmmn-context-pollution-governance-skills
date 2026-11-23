from __future__ import annotations

import ast
import json
import sys
import unittest
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from conversation_cleaner.database_handoff_adapter import (  # noqa: E402
    HANDOFF_ONLY_FIELDS,
    MEMORY_WRITE_INPUT_FIELDS,
    DatabaseHandoffAdapterError,
    to_memory_write_input,
)
from conversation_cleaner.handoff_approval import create_handoff_approval  # noqa: E402
from conversation_cleaner.handoff_contracts import MemoryWriteHandoffItem  # noqa: E402


HANDOFF_ID = "handoff_adapter_test"
REVIEWED_AT = "2026-11-22T12:00:00+08:00"


def _item_values(*, supersedes: bool = False, evidence_status: str = "verified") -> dict:
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
        "content": "Already upgraded to 32GB.",
        "importance": "high",
        "declared_sensitivity": "private",
        "requested_visibility": "private",
        "assertion_source": "user_report",
        "verification_state": "quote_verified",
        "effective_from": None,
        "effective_to": None,
        "source_id": "src_test",
        "source_unit_id": "su_new",
        "source_scope": {"scope_type": "conversation_message", "ref": "msg_new", "start": 0, "end": 25, "locator_json": {"message_id": "msg_new"}},
        "confidence": 0.95,
        "supersedes_ref": "obs_old" if supersedes else None,
        "supersedes_id": None,
        "expected_version": None,
        "domain_payload": {"existing": "kept"},
        "evidences": [
            {
                "evidence_id": "ev_new",
                "source_unit_id": "su_new",
                "quote": "Already upgraded to 32GB.",
                "locator_json": {"message_id": "msg_new", "char_start": 0, "char_end": 25},
                "status": evidence_status,
            }
        ],
        "claim_key": "claim:memory-upgrade",
        "scope_key": "workspace:ws_test|project:project_test|subject:device-memory",
        "observed_at": "2026-11-22T10:00:00+08:00",
        "transition_kind": "supersedes" if supersedes else "new",
    }


def _item(*, supersedes: bool = False, evidence_status: str = "verified") -> MemoryWriteHandoffItem:
    return MemoryWriteHandoffItem.from_dict(_item_values(supersedes=supersedes, evidence_status=evidence_status))


def _approval(item: MemoryWriteHandoffItem | dict, *, decision: str = "approve"):
    return create_handoff_approval(
        item=item,
        handoff_id=HANDOFF_ID,
        decision=decision,
        reviewer_id="reviewer-1",
        reason="Controlled adapter approval test.",
        reviewed_at=REVIEWED_AT,
    )


def _convert(item: MemoryWriteHandoffItem, **kwargs) -> dict:
    return to_memory_write_input(
        item,
        handoff_id=HANDOFF_ID,
        approval=_approval(item),
        **kwargs,
    )


class DatabaseHandoffAdapterTests(unittest.TestCase):
    def test_normal_field_mapping_and_exact_top_level_fields(self) -> None:
        item = _item()
        payload = _convert(item)
        self.assertEqual(set(payload), MEMORY_WRITE_INPUT_FIELDS)
        self.assertEqual(payload["idempotency_key"], item.idempotency_key)
        self.assertEqual(payload["operation_id"], item.operation_id)
        self.assertEqual(payload["workspace_id"], item.workspace_id)
        self.assertEqual(payload["content"], item.content)
        self.assertEqual(payload["source_scope"], item.source_scope.to_dict())
        self.assertEqual(payload["domain_payload"]["existing"], "kept")
        json.dumps(payload, ensure_ascii=False)

    def test_handoff_only_fields_and_approval_are_nested_in_domain_payload(self) -> None:
        item = _item(supersedes=True)
        approval = _approval(item)
        payload = to_memory_write_input(
            item,
            handoff_id=HANDOFF_ID,
            approval=approval,
            supersedes_ref_to_id={"obs_old": "mem_old"},
        )
        self.assertTrue(HANDOFF_ONLY_FIELDS.isdisjoint(payload))
        handoff = payload["domain_payload"]["conversation_handoff"]
        self.assertEqual(handoff["claim_key"], item.claim_key)
        self.assertEqual(handoff["scope_key"], item.scope_key)
        self.assertEqual(handoff["observed_at"], item.observed_at)
        self.assertEqual(handoff["transition_kind"], "supersedes")
        self.assertEqual(handoff["supersedes_ref"], "obs_old")
        self.assertEqual(handoff["approval_id"], approval.approval_id)
        self.assertEqual(handoff["contract_version"], "conversation-memory-handoff-v1")
        self.assertEqual(payload["supersedes_id"], "mem_old")
        self.assertIsNone(payload["expected_version"])

    def test_approval_is_required_and_reject_or_defer_is_blocked(self) -> None:
        item = _item()
        with self.assertRaisesRegex(DatabaseHandoffAdapterError, "approval rejected"):
            to_memory_write_input(item, handoff_id=HANDOFF_ID, approval=None)
        for decision in ("reject", "defer"):
            with self.subTest(decision=decision):
                with self.assertRaisesRegex(DatabaseHandoffAdapterError, "only an approve decision"):
                    to_memory_write_input(
                        item,
                        handoff_id=HANDOFF_ID,
                        approval=_approval(item, decision=decision),
                    )

    def test_missing_supersedes_mapping_is_rejected(self) -> None:
        item = _item(supersedes=True)
        approval = _approval(item)
        with self.assertRaisesRegex(DatabaseHandoffAdapterError, "explicit supersedes_ref_to_id"):
            to_memory_write_input(item, handoff_id=HANDOFF_ID, approval=approval)
        with self.assertRaisesRegex(DatabaseHandoffAdapterError, "no explicit memory_id mapping"):
            to_memory_write_input(
                item,
                handoff_id=HANDOFF_ID,
                approval=approval,
                supersedes_ref_to_id={"other": "mem_other"},
            )

    def test_expected_version_is_never_guessed(self) -> None:
        item = _item(supersedes=True)
        payload = _convert(item, supersedes_ref_to_id={"obs_old": "mem_old"})
        self.assertIsNone(payload["expected_version"])
        explicit = _convert(item, supersedes_ref_to_id={"obs_old": "mem_old"}, expected_version=1)
        self.assertEqual(explicit["expected_version"], 1)

    def test_conflicting_explicit_versions_or_ids_are_rejected(self) -> None:
        values = _item_values(supersedes=True)
        values["expected_version"] = 2
        values["supersedes_id"] = "mem_existing"
        item = MemoryWriteHandoffItem.from_dict(values)
        approval = _approval(item)
        with self.assertRaisesRegex(DatabaseHandoffAdapterError, "conflicts with the explicit mapping"):
            to_memory_write_input(
                item,
                handoff_id=HANDOFF_ID,
                approval=approval,
                supersedes_ref_to_id={"obs_old": "mem_other"},
            )
        with self.assertRaisesRegex(DatabaseHandoffAdapterError, "expected_version conflicts"):
            to_memory_write_input(
                item,
                handoff_id=HANDOFF_ID,
                approval=approval,
                supersedes_ref_to_id={"obs_old": "mem_existing"},
                expected_version=1,
            )

    def test_evidence_is_preserved_with_locators_and_status(self) -> None:
        item = _item(evidence_status="verified")
        payload = _convert(item)
        self.assertEqual(len(payload["evidences"]), 1)
        evidence = payload["evidences"][0]
        self.assertTrue(evidence["evidence_id"].startswith("ev_"))
        self.assertNotEqual(evidence["evidence_id"], "ev_new")
        self.assertEqual(evidence["source_unit_id"], "su_new")
        self.assertEqual(evidence["quote"], "Already upgraded to 32GB.")
        self.assertEqual(
            evidence["locator_json"],
            {"message_id": "msg_new", "char_start": 0, "char_end": 25, "handoff_evidence_id": "ev_new"},
        )
        self.assertEqual(evidence["status"], "verified")

    def test_unknown_or_invalid_input_is_blocked(self) -> None:
        valid_approval = _approval(_item())

        values = _item_values()
        values["unknown"] = True
        with self.assertRaisesRegex(DatabaseHandoffAdapterError, "strict contract"):
            to_memory_write_input(values, handoff_id=HANDOFF_ID, approval=valid_approval)

        values = _item_values()
        values["transition_kind"] = "invalid"
        with self.assertRaisesRegex(DatabaseHandoffAdapterError, "strict contract"):
            to_memory_write_input(values, handoff_id=HANDOFF_ID, approval=valid_approval)

        values = _item_values()
        values["domain_payload"] = {"conversation_handoff": {"forged": True}}
        reserved_item = MemoryWriteHandoffItem.from_dict(values)
        with self.assertRaisesRegex(DatabaseHandoffAdapterError, "reserved conversation_handoff"):
            to_memory_write_input(
                reserved_item,
                handoff_id=HANDOFF_ID,
                approval=_approval(reserved_item),
            )

    def test_binding_mismatch_is_blocked_by_adapter(self) -> None:
        item = _item()
        approval = _approval(item)
        with self.assertRaisesRegex(DatabaseHandoffAdapterError, "handoff_id does not match"):
            to_memory_write_input(item, handoff_id="handoff_other", approval=approval)

    def test_security_fields_are_absent(self) -> None:
        payload = _convert(_item())
        for field in ("owner_principal_id", "sensitivity", "visibility", "status", "memory_id", "version", "content_hash", "source_unit_hash"):
            self.assertNotIn(field, payload)

    def test_runtime_module_has_no_database_framework_imports(self) -> None:
        path = SCRIPTS / "conversation_cleaner" / "database_handoff_adapter.py"
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        modules: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules.add(node.module.split(".", 1)[0])
        self.assertNotIn("memory_core", modules)
        self.assertNotIn("pydantic", modules)
        for fragment in ("import pydantic", "from pydantic", "import memory_core", "from memory_core"):
            self.assertNotIn(fragment, source)


if __name__ == "__main__":
    unittest.main()
