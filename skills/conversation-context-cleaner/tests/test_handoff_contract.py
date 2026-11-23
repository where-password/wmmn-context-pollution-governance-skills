from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from conversation_cleaner.handoff_contracts import (  # noqa: E402
    ASSERTION_SOURCES,
    EVIDENCE_STATUSES,
    HANDOFF_CONTRACT_VERSION,
    IMPORTANCE_LEVELS,
    MEMORY_TYPES,
    PENDING_TRANSITION_KINDS,
    REVIEW_STATUSES,
    SENSITIVITY_LEVELS,
    STATE_KINDS,
    TRANSITION_KINDS,
    VERIFICATION_STATES,
    VISIBILITY_LEVELS,
    HandoffContractError,
    HandoffEvidence,
    HandoffManifest,
    HandoffSourceScope,
    MemoryObservation,
    MemoryWriteHandoffItem,
    PendingReviewItem,
    StateTransition,
    generate_idempotency_key,
    materialize_idempotency_key,
)


SCHEMA_PATH = SKILL_ROOT / "references" / "memory-handoff-v1.schema.json"


def _evidence(evidence_id: str = "ev_001", quote: str = "I already upgraded.") -> dict:
    return {
        "evidence_id": evidence_id,
        "source_unit_id": "su_" + evidence_id,
        "quote": quote,
        "locator_json": {"message_id": "msg_001", "char_start": 0, "char_end": len(quote)},
        "status": "verified",
    }


def _observation_values() -> dict:
    return {
        "observation_id": "obs_001",
        "conversation_id": "conv_001",
        "source_sequence": 12,
        "observed_at": "2026-11-22T10:00:00+08:00",
        "claim_key": "claim:upgrade-state",
        "scope_key": "workspace:ws_1|project:p_1|subject:upgrade",
        "memory_type": "fact",
        "content": "I already upgraded.",
        "assertion_source": "user_report",
        "verification_state": "quote_verified",
        "epistemic_status": "user_asserted",
        "state_kind": "fact",
        "evidences": [_evidence()],
        "confidence": 0.95,
        "effective_from": "2026-11-22T10:00:00+08:00",
        "effective_to": None,
        "review_status": "pending_review",
    }


def _transition_values() -> dict:
    return {
        "transition_id": "tr_001",
        "claim_key": "claim:upgrade-state",
        "scope_key": "workspace:ws_1|project:p_1|subject:upgrade",
        "to_observation_id": "obs_002",
        "transition_kind": "supersedes",
        "reason": "A later explicit user fact supersedes the earlier discussion state.",
        "evidences": [
            _evidence("ev_old", "Do not upgrade; I have no money."),
            _evidence("ev_new", "I already upgraded."),
        ],
        "from_observation_id": "obs_001",
        "confidence": 0.98,
        "review_status": "pending_review",
    }


def _write_values() -> dict:
    return {
        "idempotency_key": "mwh_pending",
        "operation_id": "op_handoff_001",
        "workspace_id": "ws_1",
        "project_id": "p_1",
        "task_id": "task_1",
        "conversation_id": "conv_001",
        "domain_profile": "generic",
        "profile_version": "1.0.0",
        "memory_type": "fact",
        "subtype": "state_change",
        "subject": "upgrade",
        "entity_type": "system",
        "entity_id": "local-device",
        "content": "I already upgraded.",
        "importance": "high",
        "declared_sensitivity": "private",
        "requested_visibility": "workspace",
        "assertion_source": "user_report",
        "verification_state": "quote_verified",
        "effective_from": "2026-11-22T10:00:00+08:00",
        "effective_to": None,
        "source_id": "src_conversation_001",
        "source_unit_id": "su_ev_new",
        "source_scope": {
            "scope_type": "conversation_message",
            "ref": "conv_001:msg_001",
            "start": 0,
            "end": 21,
            "locator_json": {"message_id": "msg_001"},
        },
        "confidence": 0.98,
        "supersedes_ref": "obs_001",
        "supersedes_id": None,
        "expected_version": 2,
        "domain_payload": {"previous_upgrade_state": "not_upgraded", "current_upgrade_state": "upgraded"},
        "evidences": [
            _evidence("ev_old", "Do not upgrade; I have no money."),
            _evidence("ev_new", "I already upgraded."),
        ],
        "claim_key": "claim:upgrade-state",
        "scope_key": "workspace:ws_1|project:p_1|subject:upgrade",
        "observed_at": "2026-11-22T10:00:00+08:00",
        "transition_kind": "supersedes",
    }


def _pending_values() -> dict:
    return {
        "review_item_id": "review_001",
        "claim_key": "claim:upgrade-state",
        "scope_key": "workspace:ws_1|project:p_1|subject:upgrade",
        "reason": "The later statement is ambiguous.",
        "transition_kind": "needs_review",
        "evidences": [_evidence()],
        "observation_id": "obs_002",
        "conversation_id": "conv_001",
        "content": "Maybe I upgraded, I am not sure.",
        "related_observation_ids": ["obs_001"],
        "review_status": "needs_review",
        "suggested_action": "manual_review_required",
    }


def _manifest_values() -> dict:
    return {
        "handoff_id": "handoff_001",
        "created_at": "2026-11-22T10:01:00+08:00",
        "plan_id": "ccp_001",
        "run_id": "ccr_001",
        "source_sha256": "a" * 64,
        "workspace_id": "ws_1",
        "project_id": "p_1",
        "conversation_ids": ["conv_001"],
        "domain_profile": "generic",
        "profile_version": "1.0.0",
        "artifact_hashes": {"memory_write_batch.jsonl": "b" * 64},
        "counts": {"memory_write_items": 1, "pending_review_items": 1, "transitions": 1},
        "contract_version": HANDOFF_CONTRACT_VERSION,
        "production_writes": False,
        "canonical_writes": False,
        "milvus_writes": False,
        "alias_changes": False,
        "contains_security_fields": False,
    }


SAMPLES = (
    (HandoffEvidence, _evidence()),
    (HandoffSourceScope, _write_values()["source_scope"]),
    (MemoryObservation, _observation_values()),
    (StateTransition, _transition_values()),
    (MemoryWriteHandoffItem, _write_values()),
    (PendingReviewItem, _pending_values()),
    (HandoffManifest, _manifest_values()),
)


class HandoffContractTests(unittest.TestCase):
    def test_legal_roundtrip_for_all_records(self) -> None:
        for cls, expected in SAMPLES:
            with self.subTest(record=cls.__name__):
                record = cls.from_dict(expected)
                self.assertIsInstance(record, cls)
                self.assertEqual(record.to_dict(), expected)
                json.dumps(record.to_dict(), ensure_ascii=False)

    def test_unknown_fields_are_rejected_at_every_level(self) -> None:
        for cls, values in SAMPLES:
            with self.subTest(record=cls.__name__):
                invalid = copy.deepcopy(values)
                invalid["unexpected_field"] = True
                with self.assertRaisesRegex(HandoffContractError, "unknown fields"):
                    cls.from_dict(invalid)

    def test_nested_unknown_fields_are_rejected(self) -> None:
        evidence = _evidence()
        evidence["unexpected"] = True
        with self.assertRaisesRegex(HandoffContractError, "unknown fields"):
            HandoffEvidence.from_dict(evidence)

        scope = _write_values()["source_scope"]
        scope["unexpected"] = True
        with self.assertRaisesRegex(HandoffContractError, "unknown fields"):
            HandoffSourceScope.from_dict(scope)


    def test_invalid_transition_kind_is_rejected(self) -> None:
        write_values = _write_values()
        write_values["transition_kind"] = "superseded"
        with self.assertRaisesRegex(HandoffContractError, "transition_kind"):
            MemoryWriteHandoffItem.from_dict(write_values)
        with self.assertRaisesRegex(HandoffContractError, "transition_kind"):
            MemoryWriteHandoffItem(**write_values)

        transition_values = _transition_values()
        transition_values["transition_kind"] = "replaced"
        with self.assertRaisesRegex(HandoffContractError, "transition_kind"):
            StateTransition.from_dict(transition_values)

        pending_values = _pending_values()
        pending_values["transition_kind"] = "new"
        with self.assertRaisesRegex(HandoffContractError, "transition_kind"):
            PendingReviewItem.from_dict(pending_values)

    def test_new_transition_rejects_prior_observation(self) -> None:
        values = _transition_values()
        values["transition_kind"] = "new"
        values["from_observation_id"] = "obs_001"
        with self.assertRaisesRegex(HandoffContractError, "new transitions"):
            StateTransition.from_dict(values)

    def test_idempotency_key_is_stable_and_sensitive(self) -> None:
        base = _write_values()
        base.pop("idempotency_key")
        first = generate_idempotency_key(base)
        second = generate_idempotency_key(copy.deepcopy(base))
        self.assertEqual(first, second)
        self.assertTrue(first.startswith("mwh_"))

        reordered = copy.deepcopy(base)
        reordered["evidences"] = list(reversed(reordered["evidences"]))
        self.assertEqual(generate_idempotency_key(reordered), first)

        same_instant = copy.deepcopy(base)
        same_instant["observed_at"] = "2026-11-22T02:00:00Z"
        same_instant["effective_from"] = "2026-11-22T02:00:00Z"
        self.assertEqual(generate_idempotency_key(same_instant), first)

        mutations = {
            "operation": lambda value: value.__setitem__("operation_id", "op_handoff_002"),
            "workspace": lambda value: value.__setitem__("workspace_id", "ws_2"),
            "source_plan_identity": lambda value: value.__setitem__("source_unit_id", "su_new_source"),
            "profile": lambda value: value.__setitem__("profile_version", "1.0.1"),
            "claim": lambda value: value.__setitem__("claim_key", "claim:different"),
            "scope": lambda value: value.__setitem__("scope_key", "workspace:ws_1|project:p_2"),
            "conversation": lambda value: value.__setitem__("conversation_id", "conv_002"),
            "content": lambda value: value.__setitem__("content", "I upgraded yesterday."),
            "transition": lambda value: value.__setitem__("transition_kind", "unchanged"),
            "observed_at": lambda value: value.__setitem__("observed_at", "2026-11-22T11:00:00+08:00"),
            "effective_from": lambda value: value.__setitem__("effective_from", "2026-11-22T11:00:00+08:00"),
            "importance": lambda value: value.__setitem__("importance", "critical"),
            "sensitivity": lambda value: value.__setitem__("declared_sensitivity", "internal"),
            "visibility": lambda value: value.__setitem__("requested_visibility", "private"),
            "assertion": lambda value: value.__setitem__("assertion_source", "user_decision"),
            "verification": lambda value: value.__setitem__("verification_state", "user_confirmed"),
            "confidence": lambda value: value.__setitem__("confidence", 0.5),
            "expected_version": lambda value: value.__setitem__("expected_version", 3),
            "payload": lambda value: value["domain_payload"].__setitem__("current_upgrade_state", "rolled_back"),
            "evidence_quote": lambda value: value["evidences"][0].__setitem__("quote", "Do not upgrade."),
            "evidence_status": lambda value: value["evidences"][0].__setitem__("status", "unresolved"),
            "scope_ref": lambda value: value["source_scope"].__setitem__("ref", "conv_001:msg_002"),
        }
        for label, mutate in mutations.items():
            with self.subTest(mutation=label):
                changed = copy.deepcopy(base)
                mutate(changed)
                self.assertNotEqual(generate_idempotency_key(changed), first)

    def test_different_conversation_and_scope_have_different_keys(self) -> None:
        base = _write_values()
        base.pop("idempotency_key")
        different_conversation = copy.deepcopy(base)
        different_conversation["conversation_id"] = "conv_002"
        different_scope = copy.deepcopy(base)
        different_scope["scope_key"] = "workspace:ws_1|project:p_1|subject:different"
        different_source_scope = copy.deepcopy(base)
        different_source_scope["source_scope"]["ref"] = "conv_001:msg_099"

        base_key = generate_idempotency_key(base)
        self.assertNotEqual(generate_idempotency_key(different_conversation), base_key)
        self.assertNotEqual(generate_idempotency_key(different_scope), base_key)
        self.assertNotEqual(generate_idempotency_key(different_source_scope), base_key)

    def test_materialize_idempotency_key_returns_valid_item(self) -> None:
        values = _write_values()
        values.pop("idempotency_key")
        item = materialize_idempotency_key(values)
        self.assertEqual(item.idempotency_key, generate_idempotency_key(values))
        self.assertEqual(MemoryWriteHandoffItem.from_dict(item.to_dict()).to_dict(), item.to_dict())

    def test_memory_write_item_covers_database_input_core_fields(self) -> None:
        expected_core_fields = {
            "idempotency_key", "operation_id", "workspace_id", "project_id", "task_id",
            "conversation_id", "domain_profile", "profile_version", "memory_type", "subtype",
            "subject", "entity_type", "entity_id", "content", "importance", "declared_sensitivity",
            "requested_visibility", "assertion_source", "verification_state", "effective_from",
            "effective_to", "source_id", "source_unit_id", "source_scope", "confidence",
            "supersedes_ref", "supersedes_id", "expected_version", "domain_payload", "evidences",
        }
        handoff_fields = {"claim_key", "scope_key", "observed_at", "transition_kind"}
        self.assertEqual(MemoryWriteHandoffItem._FIELDS, expected_core_fields | handoff_fields)
        self.assertTrue(expected_core_fields.isdisjoint(handoff_fields))

        forbidden_security_fields = {
            "owner_principal_id", "sensitivity", "visibility", "status", "memory_id", "version",
            "deleted_at", "delete_reason",
        }
        self.assertTrue(forbidden_security_fields.isdisjoint(MemoryWriteHandoffItem._FIELDS))

    def test_manifest_requires_nonempty_unique_conversation_ids(self) -> None:
        missing = _manifest_values()
        missing.pop("conversation_ids")
        with self.assertRaisesRegex(HandoffContractError, "missing required fields"):
            HandoffManifest.from_dict(missing)

        empty = _manifest_values()
        empty["conversation_ids"] = []
        with self.assertRaisesRegex(HandoffContractError, "must not be empty"):
            HandoffManifest.from_dict(empty)

        duplicate = _manifest_values()
        duplicate["conversation_ids"] = ["conv_001", "conv_001"]
        with self.assertRaisesRegex(HandoffContractError, "must not contain duplicates"):
            HandoffManifest.from_dict(duplicate)

    def test_manifest_rejects_write_or_security_claims(self) -> None:
        for field_name in (
            "production_writes", "canonical_writes", "milvus_writes", "alias_changes", "contains_security_fields",
        ):
            with self.subTest(field=field_name):
                values = _manifest_values()
                values[field_name] = True
                with self.assertRaisesRegex(HandoffContractError, field_name):
                    HandoffManifest.from_dict(values)

    def test_schema_matches_python_field_sets_and_enums(self) -> None:
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        self.assertEqual(schema["title"], HANDOFF_CONTRACT_VERSION)
        self.assertEqual(schema["x-contract-version"], HANDOFF_CONTRACT_VERSION)
        self.assertEqual(schema["$defs"]["HandoffManifest"]["properties"]["contract_version"]["const"], HANDOFF_CONTRACT_VERSION)

        mapping = {
            "HandoffEvidence": HandoffEvidence,
            "HandoffSourceScope": HandoffSourceScope,
            "MemoryObservation": MemoryObservation,
            "StateTransition": StateTransition,
            "MemoryWriteHandoffItem": MemoryWriteHandoffItem,
            "PendingReviewItem": PendingReviewItem,
            "HandoffManifest": HandoffManifest,
        }
        for name, cls in mapping.items():
            with self.subTest(record=name):
                definition = schema["$defs"][name]
                self.assertFalse(definition["additionalProperties"])
                self.assertEqual(set(definition["properties"]), set(cls._FIELDS))
                self.assertEqual(set(definition["required"]), set(cls._REQUIRED))

        defs = schema["$defs"]
        self.assertEqual(defs["HandoffEvidence"]["properties"]["status"]["enum"], list(EVIDENCE_STATUSES))
        self.assertEqual(defs["MemoryObservation"]["properties"]["memory_type"]["enum"], list(MEMORY_TYPES))
        self.assertEqual(defs["MemoryObservation"]["properties"]["state_kind"]["enum"], list(STATE_KINDS))
        self.assertEqual(defs["MemoryObservation"]["properties"]["assertion_source"]["enum"], list(ASSERTION_SOURCES))
        self.assertEqual(defs["MemoryObservation"]["properties"]["verification_state"]["enum"], list(VERIFICATION_STATES))
        self.assertEqual(defs["MemoryObservation"]["properties"]["review_status"]["enum"], list(REVIEW_STATUSES))
        self.assertEqual(defs["StateTransition"]["properties"]["transition_kind"]["enum"], list(TRANSITION_KINDS))
        self.assertEqual(defs["MemoryWriteHandoffItem"]["properties"]["memory_type"]["enum"], list(MEMORY_TYPES))
        self.assertEqual(defs["MemoryWriteHandoffItem"]["properties"]["importance"]["enum"], list(IMPORTANCE_LEVELS))
        self.assertEqual(defs["MemoryWriteHandoffItem"]["properties"]["declared_sensitivity"]["enum"], list(SENSITIVITY_LEVELS))
        self.assertEqual(defs["MemoryWriteHandoffItem"]["properties"]["requested_visibility"]["enum"], list(VISIBILITY_LEVELS))
        self.assertEqual(defs["MemoryWriteHandoffItem"]["properties"]["assertion_source"]["enum"], list(ASSERTION_SOURCES))
        self.assertEqual(defs["MemoryWriteHandoffItem"]["properties"]["verification_state"]["enum"], list(VERIFICATION_STATES))
        self.assertEqual(defs["MemoryWriteHandoffItem"]["properties"]["transition_kind"]["enum"], list(TRANSITION_KINDS))
        self.assertEqual(defs["PendingReviewItem"]["properties"]["transition_kind"]["enum"], list(PENDING_TRANSITION_KINDS))

    def test_missing_required_and_empty_evidence_are_rejected(self) -> None:
        values = _observation_values()
        values.pop("claim_key")
        with self.assertRaisesRegex(HandoffContractError, "missing required fields"):
            MemoryObservation.from_dict(values)

        write_values = _write_values()
        write_values["evidences"] = []
        with self.assertRaisesRegex(HandoffContractError, "evidences must not be empty"):
            MemoryWriteHandoffItem.from_dict(write_values)


if __name__ == "__main__":
    unittest.main()
