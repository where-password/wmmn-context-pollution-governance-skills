from __future__ import annotations

import sys
import unittest
from collections.abc import Mapping
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from conversation_cleaner.handoff_builder import (  # noqa: E402
    HandoffBuildError,
    HandoffBuildResult,
    build_handoff,
)
from conversation_cleaner.handoff_contracts import (  # noqa: E402
    HandoffEvidence,
    MemoryObservation,
    MemoryWriteHandoffItem,
    StateTransition,
)
from conversation_cleaner.state_resolution import resolve_state_transitions  # noqa: E402


SCOPE_A = "workspace:7:ws_test|project:8:project_test|subject:7:upgrade"
SCOPE_B = "workspace:7:ws_test|project:8:project_test|subject:7:install"
CLAIM = "claim:upgrade-state"


def _evidence(evidence_id: str, quote: str) -> HandoffEvidence:
    return HandoffEvidence.from_dict(
        {
            "evidence_id": evidence_id,
            "source_unit_id": "su_" + evidence_id,
            "quote": quote,
            "locator_json": {"message_id": "msg_" + evidence_id},
            "status": "verified",
        }
    )


def _obs(
    observation_id: str,
    content: str,
    state_kind: str,
    observed_at: str,
    source_sequence: int,
    *,
    conversation_id: str = "conv_001",
    scope_key: str = SCOPE_A,
    claim_key: str = CLAIM,
    evidence_id: str | None = None,
) -> MemoryObservation:
    return MemoryObservation.from_dict(
        {
            "observation_id": observation_id,
            "conversation_id": conversation_id,
            "source_sequence": source_sequence,
            "observed_at": observed_at,
            "claim_key": claim_key,
            "scope_key": scope_key,
            "memory_type": "constraint" if state_kind == "constraint" else "fact",
            "content": content,
            "assertion_source": "user_report",
            "verification_state": "quote_verified",
            "epistemic_status": "user_asserted",
            "state_kind": state_kind,
            "evidences": [_evidence(evidence_id or ("ev_" + observation_id), content).to_dict()],
            "confidence": 0.93,
            "effective_from": "2026-11-22T00:00:00+08:00",
            "effective_to": None,
            "review_status": "pending_review",
        }
    )


def _resolve(observations: list[MemoryObservation]):
    return resolve_state_transitions(observations)


def _build(observations, transitions) -> HandoffBuildResult:
    return build_handoff(
        observations,
        transitions,
        workspace_id="ws_test",
        project_id="project_test",
        domain_profile="generic",
        profile_version="1.0.0",
        source_id="src_conversation_test",
    )


class HandoffBuilderTests(unittest.TestCase):
    def test_new_transition_produces_one_database_write_item(self) -> None:
        observation = _obs("obs_new", "I already upgraded.", "fact", "2026-11-22T10:00:00+08:00", 1)
        resolved = _resolve([observation])
        result = _build(resolved.observations, resolved.transitions)

        self.assertIsInstance(result, HandoffBuildResult)
        self.assertIsInstance(result.counts, Mapping)
        self.assertEqual(result.counts["write_items"], 1)
        self.assertEqual(result.counts["pending_review_items"], 0)
        self.assertEqual(result.counts["skipped_observations"], 0)
        self.assertEqual(len(result.write_items), 1)
        item = result.write_items[0]
        self.assertIsInstance(item, MemoryWriteHandoffItem)
        self.assertEqual(item.content, observation.content)
        self.assertEqual(item.claim_key, observation.claim_key)
        self.assertEqual(item.scope_key, observation.scope_key)
        self.assertEqual(item.observed_at, observation.observed_at)
        self.assertEqual(item.transition_kind, "new")
        self.assertIsNone(item.supersedes_ref)
        self.assertIsNone(item.supersedes_id)
        self.assertEqual(item.assertion_source, observation.assertion_source)
        self.assertEqual(item.verification_state, observation.verification_state)
        self.assertEqual(item.domain_payload["epistemic_status"], observation.epistemic_status)
        self.assertTrue(item.operation_id.startswith("op_"))
        self.assertTrue(item.idempotency_key.startswith("mwh_"))
        self.assertEqual({evidence.evidence_id for evidence in item.evidences}, {"ev_obs_new"})

    def test_supersedes_writes_only_new_current_and_preserves_both_evidences(self) -> None:
        old = _obs("obs_old", "Do not upgrade; I have no money.", "constraint", "2026-11-22T10:00:00+08:00", 1)
        new = _obs("obs_new", "I already upgraded.", "fact", "2026-11-22T11:00:00+08:00", 2)
        resolved = _resolve([new, old])
        result = _build(resolved.observations, resolved.transitions)

        self.assertEqual(len(result.write_items), 1)
        item = result.write_items[0]
        self.assertEqual(item.content, "I already upgraded.")
        self.assertEqual(item.transition_kind, "supersedes")
        self.assertEqual(item.supersedes_ref, "obs_old")
        self.assertIsNone(item.supersedes_id)
        self.assertEqual({evidence.evidence_id for evidence in item.evidences}, {"ev_obs_old", "ev_obs_new"})
        self.assertEqual(len(result.skipped_observations), 1)
        skipped = result.skipped_observations[0]
        self.assertEqual(skipped.observation_id, "obs_old")
        self.assertIn("superseded", skipped.reason.casefold())

    def test_conflict_and_needs_review_never_write_the_review_observation(self) -> None:
        for later_kind, content in (
            ("conflict", "Sources disagree about whether I upgraded."),
            ("fact", "Maybe I upgraded, but I am not sure."),
        ):
            with self.subTest(later_kind=later_kind):
                old = _obs("obs_old", "I did not upgrade.", "fact", "2026-11-22T10:00:00+08:00", 1)
                later = _obs("obs_later", content, later_kind, "2026-11-22T11:00:00+08:00", 2)
                resolved = _resolve([old, later])
                result = _build(resolved.observations, resolved.transitions)
                self.assertNotIn("obs_later", {item.domain_payload.get("memory_observation_id") for item in result.write_items})
                self.assertEqual(len(result.pending_review_items), 1)
                review = result.pending_review_items[0]
                self.assertEqual(review.observation_id, "obs_later")
                self.assertIn(review.transition_kind, {"conflict", "needs_review"})
                self.assertEqual({evidence.evidence_id for evidence in review.evidences}, {"ev_obs_old", "ev_obs_later"})

    def test_unchanged_and_historical_only_observations_are_skipped(self) -> None:
        old = _obs("obs_old", "I already upgraded.", "fact", "2026-11-22T10:00:00+08:00", 1)
        duplicate = _obs("obs_duplicate", "I already upgraded.", "fact", "2026-11-22T11:00:00+08:00", 2)
        question = _obs("obs_question", "Should I upgrade again?", "discussion", "2026-11-22T12:00:00+08:00", 3)
        resolved = _resolve([question, duplicate, old])
        result = _build(resolved.observations, resolved.transitions)

        self.assertEqual([item.content for item in result.write_items], ["I already upgraded."])
        skipped_by_id = {item.observation_id: item for item in result.skipped_observations}
        self.assertEqual(skipped_by_id["obs_duplicate"].transition_kind, "unchanged")
        self.assertEqual(skipped_by_id["obs_question"].transition_kind, "historical_only")

    def test_observation_without_transition_goes_to_review(self) -> None:
        orphan = _obs("obs_orphan", "Unverified state without a transition.", "fact", "2026-11-22T10:00:00+08:00", 1)
        result = _build([orphan], [])
        self.assertEqual(result.write_items, ())
        self.assertEqual(len(result.pending_review_items), 1)
        review = result.pending_review_items[0]
        self.assertEqual(review.observation_id, "obs_orphan")
        self.assertEqual(review.transition_kind, "needs_review")
        self.assertIn("no matching state transition", review.reason.casefold())
        self.assertEqual({evidence.evidence_id for evidence in review.evidences}, {"ev_obs_orphan"})


    def test_build_is_idempotent_and_independent_of_input_order(self) -> None:
        first_conv = _obs(
            "obs_conv_a",
            "Conversation A state.",
            "fact",
            "2026-11-22T10:00:00+08:00",
            2,
            conversation_id="conv_A",
        )
        second_conv = _obs(
            "obs_conv_b",
            "Conversation B state.",
            "fact",
            "2026-11-22T10:00:00+08:00",
            1,
            conversation_id="conv_B",
        )
        resolved = _resolve([second_conv, first_conv])
        forward = _build(resolved.observations, resolved.transitions)
        repeated = _build(resolved.observations, resolved.transitions)
        reversed_result = _build(
            list(reversed(resolved.observations)),
            list(reversed(resolved.transitions)),
        )
        self.assertEqual(forward, repeated)
        self.assertEqual(forward.to_dict(), reversed_result.to_dict())
        self.assertEqual(
            [item.conversation_id for item in forward.write_items],
            ["conv_A", "conv_B"],
        )
        self.assertEqual(
            [item.source_sequence for item in forward.skipped_observations],
            [],
        )

    def test_cross_conversation_and_cross_scope_never_form_supersede(self) -> None:
        cross_conversation = _resolve(
            [
                _obs("obs_a", "State A.", "fact", "2026-11-22T10:00:00+08:00", 1, conversation_id="conv_A"),
                _obs("obs_b", "State B.", "fact", "2026-11-22T11:00:00+08:00", 2, conversation_id="conv_B"),
            ]
        )
        conversation_result = _build(cross_conversation.observations, cross_conversation.transitions)
        self.assertEqual(len(conversation_result.write_items), 2)
        self.assertEqual({item.transition_kind for item in conversation_result.write_items}, {"new"})
        self.assertTrue(all(item.supersedes_ref is None for item in conversation_result.write_items))

        cross_scope = _resolve(
            [
                _obs("obs_scope_a", "Scope A state.", "fact", "2026-11-22T10:00:00+08:00", 1, scope_key=SCOPE_A),
                _obs("obs_scope_b", "Scope B state.", "fact", "2026-11-22T11:00:00+08:00", 2, scope_key=SCOPE_B),
            ]
        )
        scope_result = _build(cross_scope.observations, cross_scope.transitions)
        self.assertEqual(len(scope_result.write_items), 2)
        self.assertEqual({item.transition_kind for item in scope_result.write_items}, {"new"})
        self.assertTrue(all(item.supersedes_ref is None for item in scope_result.write_items))

    def test_cross_chain_transition_is_rejected(self) -> None:
        first = _obs("obs_conv_a", "State A.", "fact", "2026-11-22T10:00:00+08:00", 1, conversation_id="conv_A")
        second = _obs("obs_conv_b", "State B.", "fact", "2026-11-22T11:00:00+08:00", 2, conversation_id="conv_B")
        cross_transition = StateTransition.from_dict(
            {
                "transition_id": "tr_cross_chain",
                "claim_key": second.claim_key,
                "scope_key": second.scope_key,
                "to_observation_id": second.observation_id,
                "transition_kind": "supersedes",
                "reason": "Invalid cross-conversation test transition.",
                "evidences": [item.to_dict() for item in (*first.evidences, *second.evidences)],
                "from_observation_id": first.observation_id,
                "confidence": 0.9,
                "review_status": "pending_review",
            }
        )
        with self.assertRaisesRegex(HandoffBuildError, "crosses conversation"):
            _build([first, second], [cross_transition])

    def test_pending_review_order_is_deterministic(self) -> None:
        later = _obs("obs_b", "Orphan B.", "fact", "2026-11-22T10:00:00+08:00", 2, conversation_id="conv_B")
        earlier = _obs("obs_a", "Orphan A.", "fact", "2026-11-22T10:00:00+08:00", 1, conversation_id="conv_A")
        first = _build([later, earlier], [])
        second = _build([earlier, later], [])
        self.assertEqual(first.to_dict(), second.to_dict())
        self.assertEqual([item.conversation_id for item in first.pending_review_items], ["conv_A", "conv_B"])
        self.assertEqual(first.counts["pending_review_items"], 2)

    def test_missing_transition_target_is_rejected(self) -> None:
        observation = _obs("obs_known", "Known state.", "fact", "2026-11-22T10:00:00+08:00", 1)
        transition = StateTransition.from_dict(
            {
                "transition_id": "tr_missing",
                "claim_key": observation.claim_key,
                "scope_key": observation.scope_key,
                "to_observation_id": "obs_missing",
                "transition_kind": "new",
                "reason": "Missing target test.",
                "evidences": [item.to_dict() for item in observation.evidences],
                "from_observation_id": None,
                "confidence": 0.9,
                "review_status": "pending_review",
            }
        )
        with self.assertRaisesRegex(HandoffBuildError, "target observation is missing"):
            _build([observation], [transition])

    def test_builder_source_has_no_io_clock_network_or_environment_dependency(self) -> None:
        source = (SCRIPTS / "conversation_cleaner" / "handoff_builder.py").read_text(encoding="utf-8")
        forbidden_fragments = (
            "os.environ",
            "os.getenv",
            "Path(",
            "open(",
            "socket",
            "requests",
            "urllib",
            "datetime.now",
            "datetime.today",
            "time.time",
            "uuid",
            "sqlite3",
            "pymilvus",
        )
        for fragment in forbidden_fragments:
            with self.subTest(fragment=fragment):
                self.assertNotIn(fragment, source)


if __name__ == "__main__":
    unittest.main()
