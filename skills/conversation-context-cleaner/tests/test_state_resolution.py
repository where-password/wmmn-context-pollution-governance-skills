from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from conversation_cleaner.handoff_contracts import HandoffEvidence, MemoryObservation  # noqa: E402
from conversation_cleaner.state_resolution import (  # noqa: E402
    StateResolutionError,
    build_scope_key,
    candidate_to_observation,
    resolve_state_transitions,
)


DEFAULT_SCOPE = "workspace:7:ws_test|project:8:project_test|subject:7:upgrade"
DEFAULT_CLAIM = "claim:upgrade-state"


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
    scope_key: str = DEFAULT_SCOPE,
    claim_key: str = DEFAULT_CLAIM,
    evidence_id: str | None = None,
    memory_type: str | None = None,
) -> MemoryObservation:
    return MemoryObservation.from_dict(
        {
            "observation_id": observation_id,
            "conversation_id": conversation_id,
            "source_sequence": source_sequence,
            "observed_at": observed_at,
            "claim_key": claim_key,
            "scope_key": scope_key,
            "memory_type": memory_type or ("constraint" if state_kind == "constraint" else "fact"),
            "content": content,
            "assertion_source": "user_report",
            "verification_state": "quote_verified",
            "epistemic_status": "user_asserted",
            "state_kind": state_kind,
            "evidences": [_evidence(evidence_id or ("ev_" + observation_id), content).to_dict()],
            "confidence": 0.9,
            "effective_from": None,
            "effective_to": None,
            "review_status": "pending_review",
        }
    )


def _transition(result, observation_id: str):
    return next(item for item in result.transitions if item.to_observation_id == observation_id)


class StateResolutionTests(unittest.TestCase):
    def test_candidate_to_observation_is_pure_and_deterministic(self) -> None:
        candidate = {
            "candidate_id": "cand_001",
            "conversation_id": "conv_001",
            "workspace_id": "ws_test",
            "project_id": "project_test",
            "candidate_type": "decision",
            "content": "I will upgrade the local service.",
            "proposed_memory_type": "decision",
            "assertion_source": "user_decision",
            "verification_state": "user_confirmed",
            "epistemic_status": "user_asserted",
            "confidence": 0.99,
            "review_status": "pending_review",
            "evidence": [
                {
                    "evidence_id": "ev_candidate",
                    "source_unit_id": "su_candidate",
                    "quote": "I will upgrade the local service.",
                    "message_id": "msg_candidate",
                    "char_start": 0,
                    "char_end": 32,
                    "status": "verified",
                }
            ],
        }
        first = candidate_to_observation(candidate, observed_at="2026-11-22T10:00:00+08:00", source_sequence=4)
        second = candidate_to_observation(copy.deepcopy(candidate), observed_at="2026-11-22T10:00:00+08:00", source_sequence=4)
        self.assertEqual(first, second)
        self.assertEqual(first.state_kind, "decision")
        self.assertEqual(first.claim_key, "candidate:cand_001")
        self.assertEqual(first.scope_key, build_scope_key(workspace_id="ws_test", project_id="project_test"))
        self.assertEqual(first.evidences[0].quote, "I will upgrade the local service.")

        question = copy.deepcopy(candidate)
        question["content"] = "Should I upgrade?"
        question["evidence"][0]["quote"] = "Should I upgrade?"
        converted = candidate_to_observation(question, observed_at="2026-11-22T10:01:00+08:00", source_sequence=5)
        self.assertEqual(converted.state_kind, "discussion")

    def test_explicit_fact_supersedes_earlier_constraint(self) -> None:
        old = _obs("obs_old", "Do not upgrade; I have no money.", "constraint", "2026-11-22T10:00:00+08:00", 1)
        new = _obs("obs_new", "I already upgraded.", "fact", "2026-11-22T11:00:00+08:00", 2)
        result = resolve_state_transitions([new, old])
        transition = _transition(result, "obs_new")
        self.assertEqual(transition.transition_kind, "supersedes")
        self.assertEqual(transition.from_observation_id, "obs_old")
        self.assertEqual(result.current_observation_ids, ("obs_new",))
        self.assertEqual(result.superseded_observation_ids, ("obs_old",))
        self.assertEqual({item.evidence_id for item in transition.evidences}, {"ev_obs_old", "ev_obs_new"})
        self.assertIn("obs_old", {item.observation_id for item in result.observations})
        self.assertIn("obs_new", {item.observation_id for item in result.observations})

    def test_question_does_not_override_current_state(self) -> None:
        old = _obs("obs_old", "I did not upgrade.", "fact", "2026-11-22T10:00:00+08:00", 1)
        question = _obs("obs_question", "Should I upgrade?", "discussion", "2026-11-22T11:00:00+08:00", 2)
        result = resolve_state_transitions([old, question])
        self.assertEqual(_transition(result, "obs_question").transition_kind, "historical_only")
        self.assertEqual(result.current_observation_ids, ("obs_old",))
        self.assertEqual(result.pending_review_items, ())

    def test_hypothesis_and_task_do_not_override_current_state(self) -> None:
        for state_kind, content in (
            ("hypothesis", "If I save money, maybe I will upgrade."),
            ("task", "Task: decide later whether to upgrade."),
        ):
            with self.subTest(state_kind=state_kind):
                old = _obs("obs_old", "I did not upgrade.", "fact", "2026-11-22T10:00:00+08:00", 1)
                later = _obs("obs_later", content, state_kind, "2026-11-22T11:00:00+08:00", 2)
                result = resolve_state_transitions([old, later])
                self.assertEqual(_transition(result, "obs_later").transition_kind, "historical_only")
                self.assertEqual(result.current_observation_ids, ("obs_old",))

    def test_ambiguous_later_statement_requires_review(self) -> None:
        old = _obs("obs_old", "I did not upgrade.", "fact", "2026-11-22T10:00:00+08:00", 1)
        ambiguous = _obs("obs_maybe", "Maybe I upgraded, but I am not sure.", "fact", "2026-11-22T11:00:00+08:00", 2)
        result = resolve_state_transitions([old, ambiguous])
        transition = _transition(result, "obs_maybe")
        self.assertEqual(transition.transition_kind, "needs_review")
        self.assertEqual(result.current_observation_ids, ("obs_old",))
        self.assertEqual(result.needs_review_observation_ids, ("obs_maybe",))
        self.assertEqual(len(result.pending_review_items), 1)
        self.assertEqual(result.pending_review_items[0].observation_id, "obs_maybe")
        self.assertIn("obs_old", result.pending_review_items[0].related_observation_ids)

    def test_conflict_state_always_goes_to_review(self) -> None:
        old = _obs("obs_old", "I did not upgrade.", "fact", "2026-11-22T10:00:00+08:00", 1)
        conflict = _obs("obs_conflict", "Sources disagree about whether I upgraded.", "conflict", "2026-11-22T11:00:00+08:00", 2)
        result = resolve_state_transitions([old, conflict])
        self.assertEqual(_transition(result, "obs_conflict").transition_kind, "conflict")
        self.assertEqual(result.current_observation_ids, ("obs_old",))
        self.assertEqual(result.conflict_observation_ids, ("obs_conflict",))
        self.assertEqual(len(result.pending_review_items), 1)
        self.assertEqual(result.pending_review_items[0].transition_kind, "conflict")


    def test_different_conversations_never_auto_supersede(self) -> None:
        first = _obs("obs_conv_a", "I did not upgrade.", "fact", "2026-11-22T10:00:00+08:00", 1, conversation_id="conv_A")
        second = _obs("obs_conv_b", "I already upgraded.", "fact", "2026-11-22T11:00:00+08:00", 2, conversation_id="conv_B")
        result = resolve_state_transitions([second, first])
        self.assertEqual({item.transition_kind for item in result.transitions}, {"new"})
        self.assertEqual(result.current_observation_ids, ("obs_conv_a", "obs_conv_b"))
        self.assertEqual(result.superseded_observation_ids, ())
        transition_targets = {item.to_observation_id for item in result.transitions}
        self.assertEqual(transition_targets, {"obs_conv_a", "obs_conv_b"})

    def test_different_scopes_never_auto_supersede(self) -> None:
        scope_a = build_scope_key(workspace_id="ws_test", project_id="project_test", subject="upgrade-a")
        scope_b = build_scope_key(workspace_id="ws_test", project_id="project_test", subject="upgrade-b")
        first = _obs("obs_scope_a", "I did not upgrade.", "fact", "2026-11-22T10:00:00+08:00", 1, scope_key=scope_a)
        second = _obs("obs_scope_b", "I already upgraded.", "fact", "2026-11-22T11:00:00+08:00", 2, scope_key=scope_b)
        result = resolve_state_transitions([second, first])
        self.assertEqual({item.transition_kind for item in result.transitions}, {"new"})
        self.assertEqual(result.current_observation_ids, ("obs_scope_a", "obs_scope_b"))
        self.assertEqual(result.superseded_observation_ids, ())

    def test_same_time_equal_priority_conflict_is_stable_and_preserves_current(self) -> None:
        current = _obs("obs_current", "The system is not upgraded.", "fact", "2026-11-22T09:00:00+08:00", 1)
        left = _obs("obs_left", "I already upgraded.", "fact", "2026-11-22T10:00:00+08:00", 2)
        right = _obs("obs_right", "I did not upgrade.", "fact", "2026-11-22T10:00:00+08:00", 3)
        first = resolve_state_transitions([right, current, left])
        second = resolve_state_transitions([left, right, current])
        self.assertEqual(first.to_dict(), second.to_dict())
        self.assertEqual(_transition(first, "obs_left").transition_kind, "conflict")
        self.assertEqual(_transition(first, "obs_right").transition_kind, "conflict")
        self.assertEqual(first.current_observation_ids, ("obs_current",))
        self.assertEqual(first.conflict_observation_ids, ("obs_left", "obs_right"))
        self.assertEqual(len(first.pending_review_items), 2)
        for review in first.pending_review_items:
            self.assertEqual(review.transition_kind, "conflict")
            self.assertIn("obs_current", review.related_observation_ids)
            self.assertEqual(
                {item.evidence_id for item in review.evidences},
                {"ev_obs_current", "ev_obs_left", "ev_obs_right"},
            )

    def test_same_time_duplicate_selection_uses_sequence_then_id(self) -> None:
        duplicate_a = _obs("obs_b", "I already upgraded.", "fact", "2026-11-22T10:00:00+08:00", 2)
        duplicate_b = _obs("obs_a", "I already upgraded.", "fact", "2026-11-22T10:00:00+08:00", 1)
        result = resolve_state_transitions([duplicate_a, duplicate_b])
        self.assertEqual(result.current_observation_ids, ("obs_a",))
        self.assertEqual(_transition(result, "obs_a").transition_kind, "new")
        duplicate_transition = _transition(result, "obs_b")
        self.assertEqual(duplicate_transition.transition_kind, "unchanged")
        self.assertEqual(duplicate_transition.from_observation_id, "obs_a")

    def test_non_superseding_later_constraint_requires_review(self) -> None:
        old = _obs("obs_old", "Use configuration A.", "constraint", "2026-11-22T10:00:00+08:00", 1)
        later = _obs("obs_later", "Use configuration B.", "constraint", "2026-11-22T11:00:00+08:00", 2)
        result = resolve_state_transitions([old, later])
        self.assertEqual(_transition(result, "obs_later").transition_kind, "needs_review")
        self.assertEqual(result.current_observation_ids, ("obs_old",))
        self.assertEqual(result.needs_review_observation_ids, ("obs_later",))

    def test_identical_later_state_is_unchanged(self) -> None:
        old = _obs("obs_old", "I already upgraded.", "fact", "2026-11-22T10:00:00+08:00", 1)
        later = _obs("obs_later", "I already upgraded.", "fact", "2026-11-22T11:00:00+08:00", 2)
        result = resolve_state_transitions([old, later])
        self.assertEqual(_transition(result, "obs_later").transition_kind, "unchanged")
        self.assertEqual(result.current_observation_ids, ("obs_old",))

    def test_resolver_is_idempotent_and_does_not_mutate_observations(self) -> None:
        observations = [
            _obs("obs_old", "Do not upgrade; I have no money.", "constraint", "2026-11-22T10:00:00+08:00", 1),
            _obs("obs_new", "I already upgraded.", "fact", "2026-11-22T11:00:00+08:00", 2),
        ]
        before = [item.to_dict() for item in observations]
        first = resolve_state_transitions(observations)
        second = resolve_state_transitions(observations)
        self.assertEqual(first, second)
        self.assertEqual([item.to_dict() for item in observations], before)

    def test_resolver_source_has_no_io_clock_or_environment_dependencies(self) -> None:
        source = (SCRIPTS / "conversation_cleaner" / "state_resolution.py").read_text(encoding="utf-8")
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
        )
        for fragment in forbidden_fragments:
            with self.subTest(fragment=fragment):
                self.assertNotIn(fragment, source)


if __name__ == "__main__":
    unittest.main()
