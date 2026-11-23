from __future__ import annotations

import hashlib
import sys
import unittest
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from conversation_cleaner.contracts import Candidate, EvidenceSpan, Message  # noqa: E402
from conversation_cleaner.observation_generation import (  # noqa: E402
    ObservationGenerationError,
    generate_observations,
)
from conversation_cleaner.state_resolution import build_scope_key, resolve_state_transitions  # noqa: E402


CONVERSATION_ID = "conv_observation_generation"
WORKSPACE_ID = "ws_observation"
PROJECT_ID = "project_observation"
SOURCE_ID = "src_observation"
DOMAIN_PROFILE = "generic"
PROFILE_VERSION = "1.0.0"


def _message(sequence: int, text: str, *, metadata: dict | None = None, conversation_id: str = CONVERSATION_ID) -> Message:
    timestamp = f"2026-11-22T10:{sequence:02d}:00+08:00"
    source_unit_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return Message(
        conversation_id=conversation_id,
        message_id=f"msg_{sequence:03d}",
        role="user",
        timestamp=timestamp,
        timestamp_utc=timestamp,
        sequence=sequence,
        text=text,
        attachments=(),
        event_type="message",
        source_revision=None,
        metadata=metadata or {},
        source_unit_hash=source_unit_hash,
        revision_id=source_unit_hash[:16],
        source_unit_id="su_" + source_unit_hash[:20],
    )


def _candidate(
    message: Message,
    candidate_id: str,
    candidate_type: str,
    *,
    content: str | None = None,
    char_start: int = 0,
    char_end: int | None = None,
    source_unit_id: str | None = None,
) -> Candidate:
    content = message.text if content is None else content
    char_end = len(message.text) if char_end is None else char_end
    evidence = EvidenceSpan(
        evidence_id="ev_" + candidate_id,
        source_unit_id=source_unit_id or message.source_unit_id,
        message_id=message.message_id,
        role=message.role,
        quote=message.text[char_start:char_end],
        char_start=char_start,
        char_end=char_end,
        source_unit_hash=message.source_unit_hash,
        verified=True,
        status="verified",
    )
    memory_types = {
        "decision": "decision",
        "fact": "fact",
        "constraint": "constraint",
        "preference": "preference",
        "task": "task",
        "open_question": "open_question",
    }
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return Candidate(
        candidate_id=candidate_id,
        workspace_id=WORKSPACE_ID,
        project_id=PROJECT_ID,
        conversation_id=message.conversation_id,
        topic_segment_id="segment_1",
        domain_profile=DOMAIN_PROFILE,
        profile_version=PROFILE_VERSION,
        candidate_type=candidate_type,
        proposed_memory_type=memory_types.get(candidate_type, "fact"),
        content=content,
        content_hash=content_hash,
        assertion_source="user_report",
        verification_state="quote_verified",
        epistemic_status="user_asserted",
        authority_basis="user_authoritative_statement",
        importance="high",
        evidence=(evidence,),
        risk_flags=(),
        suggested_action="standard_review",
        review_status="pending_review",
        record_status="candidate",
    )


def _generate(candidates: list[Candidate], messages: list[Message], **kwargs):
    return generate_observations(
        candidates,
        messages,
        workspace_id=WORKSPACE_ID,
        project_id=PROJECT_ID,
        source_id=SOURCE_ID,
        domain_profile=DOMAIN_PROFILE,
        profile_version=PROFILE_VERSION,
        subject="device-memory",
        **kwargs,
    )


class ObservationGenerationTests(unittest.TestCase):
    def test_explicit_claim_key_produces_supersedes(self) -> None:
        claim_key = "claim:memory-upgrade"
        scope_key = "workspace:ws_observation|project:project_observation|subject:device-memory"
        old_message = _message(
            0,
            "暂时不升级内存，因为没有预算。",
            metadata={"observation": {"claim_key": claim_key, "scope_key": scope_key, "state_kind": "constraint"}},
        )
        new_message = _message(
            1,
            "已经升级到 32GB 内存。",
            metadata={"observation": {"claim_key": claim_key, "scope_key": scope_key, "state_kind": "fact"}},
        )
        candidates = [
            _candidate(old_message, "cand_old", "constraint"),
            _candidate(new_message, "cand_new", "fact"),
        ]
        generated = _generate(candidates, [new_message, old_message])
        self.assertEqual(generated.issues, ())
        self.assertEqual(len(generated.observations), 2)
        self.assertEqual({item.claim_key for item in generated.observations}, {claim_key})
        self.assertEqual({item.scope_key for item in generated.observations}, {scope_key})

        resolved = resolve_state_transitions(generated.observations)
        supersedes = [item for item in resolved.transitions if item.transition_kind == "supersedes"]
        self.assertEqual(len(supersedes), 1)
        self.assertEqual(supersedes[0].from_observation_id, generated.observations[0].observation_id)
        self.assertEqual(supersedes[0].to_observation_id, generated.observations[1].observation_id)

    def test_no_metadata_fallback_does_not_merge_claims(self) -> None:
        old_message = _message(0, "I did not upgrade.")
        new_message = _message(1, "I already upgraded.")
        candidates = [
            _candidate(old_message, "cand_old_fallback", "constraint"),
            _candidate(new_message, "cand_new_fallback", "fact"),
        ]
        generated = _generate(candidates, [old_message, new_message])
        self.assertEqual(generated.issues, ())
        self.assertEqual(
            {item.claim_key for item in generated.observations},
            {"candidate:cand_old_fallback", "candidate:cand_new_fallback"},
        )
        expected_scope = build_scope_key(
            workspace_id=WORKSPACE_ID,
            project_id=PROJECT_ID,
            subject="device-memory",
        )
        self.assertEqual({item.scope_key for item in generated.observations}, {expected_scope})
        resolved = resolve_state_transitions(generated.observations)
        self.assertFalse(any(item.transition_kind == "supersedes" for item in resolved.transitions))
        self.assertEqual(len(resolved.current_observation_ids), 2)

    def test_invalid_observation_metadata_is_rejected(self) -> None:
        invalid_values = (
            {"claim_key": "claim:x", "unknown": True},
            {"scope_key": "scope"},
            {"claim_key": ""},
            {"claim_key": "claim:x", "state_kind": "invalid"},
            {"claim_key": "claim:x", "observed_at": "2026-11-22T10:00:00"},
            {"claim_key": "claim:x", "effective_from": "2026-11-22T11:00:00+08:00", "effective_to": "2026-11-22T10:00:00+08:00"},
        )
        for metadata in invalid_values:
            with self.subTest(metadata=metadata):
                message = _message(0, "A controlled decision.", metadata={"observation": metadata})
                candidate = _candidate(message, "cand_invalid", "decision")
                with self.assertRaises(ObservationGenerationError):
                    _generate([candidate], [message])

        message = _message(0, "A controlled decision.", metadata={"observation": None})
        candidate = _candidate(message, "cand_none_metadata", "decision")
        with self.assertRaisesRegex(ObservationGenerationError, "must be an object"):
            _generate([candidate], [message])

    def test_single_message_multiple_candidates_and_one_metadata_is_ambiguous(self) -> None:
        text = "First independent claim. Second independent claim."
        message = _message(
            0,
            text,
            metadata={"observation": {"claim_key": "claim:too-broad", "state_kind": "decision"}},
        )
        first = _candidate(message, "cand_a", "decision", content=text[:24], char_start=0, char_end=24)
        second = _candidate(message, "cand_b", "decision", content=text[26:], char_start=26, char_end=len(text))
        generated = _generate([second, first], [message])
        self.assertEqual(generated.observations, ())
        self.assertEqual(len(generated.issues), 1)
        issue = generated.issues[0]
        self.assertEqual(issue.issue_code, "ambiguous_observation_metadata")
        self.assertEqual(issue.candidate_ids, ("cand_a", "cand_b"))
        self.assertEqual(issue.source_sequence, 0)

    def test_explicit_overrides_are_applied(self) -> None:
        message = _message(
            0,
            "Prefer the 32GB configuration.",
            metadata={
                "observation": {
                    "claim_key": "claim:memory-preference",
                    "scope_key": "explicit-scope",
                    "state_kind": "preference",
                    "observed_at": "2026-12-01T09:00:00+08:00",
                    "effective_from": "2026-12-01T10:00:00+08:00",
                    "effective_to": "2026-12-31T10:00:00+08:00",
                }
            },
        )
        candidate = _candidate(message, "cand_preference", "decision")
        generated = _generate([candidate], [message])
        self.assertEqual(generated.issues, ())
        observation = generated.observations[0]
        self.assertEqual(observation.claim_key, "claim:memory-preference")
        self.assertEqual(observation.scope_key, "explicit-scope")
        self.assertEqual(observation.state_kind, "preference")
        self.assertEqual(observation.observed_at, "2026-12-01T09:00:00+08:00")
        self.assertEqual(observation.effective_from, "2026-12-01T10:00:00+08:00")
        self.assertEqual(observation.effective_to, "2026-12-31T10:00:00+08:00")
        self.assertEqual(observation.source_sequence, 0)


    def test_evidence_and_message_sequence_are_preserved(self) -> None:
        text = "Context sentence. Decision: keep 32GB."
        start = text.index("Decision")
        message = _message(3, text)
        candidate = _candidate(message, "cand_evidence", "decision", content=text[start:], char_start=start, char_end=len(text))
        generated = _generate([candidate], [message])
        observation = generated.observations[0]
        self.assertEqual(observation.source_sequence, 3)
        self.assertEqual(observation.observed_at, message.timestamp)
        self.assertEqual(observation.content, text[start:])
        self.assertEqual(observation.state_kind, "decision")
        self.assertEqual(len(observation.evidences), 1)
        evidence = observation.evidences[0]
        self.assertEqual(evidence.evidence_id, "ev_cand_evidence")
        self.assertEqual(evidence.source_unit_id, message.source_unit_id)
        self.assertEqual(evidence.quote, text[start:])
        self.assertEqual(evidence.locator_json["message_id"], message.message_id)
        self.assertEqual(evidence.locator_json["char_start"], start)
        self.assertEqual(evidence.locator_json["char_end"], len(text))
        self.assertEqual(evidence.status, "verified")

    def test_generation_is_order_independent_and_idempotent(self) -> None:
        first_message = _message(0, "I did not upgrade.")
        second_message = _message(1, "I already upgraded.")
        first = _candidate(first_message, "cand_first", "constraint")
        second = _candidate(second_message, "cand_second", "fact")
        forward = _generate([first, second], [first_message, second_message])
        reverse = _generate([second, first], [second_message, first_message])
        repeated = _generate([first, second], [first_message, second_message])
        self.assertEqual(forward.to_dict(), reverse.to_dict())
        self.assertEqual(forward, repeated)
        self.assertEqual(
            [(item.conversation_id, item.source_sequence) for item in forward.observations],
            [(CONVERSATION_ID, 0), (CONVERSATION_ID, 1)],
        )

    def test_candidate_message_and_source_unit_mismatch_is_rejected(self) -> None:
        message = _message(0, "A decision.")
        mismatched = _candidate(message, "cand_mismatch", "decision", source_unit_id="su_forged")
        with self.assertRaisesRegex(ObservationGenerationError, "source_unit_id/message_id conflict"):
            _generate([mismatched], [message])

    def test_generation_source_has_no_io_clock_or_environment_dependency(self) -> None:
        source = (SCRIPTS / "conversation_cleaner" / "observation_generation.py").read_text(encoding="utf-8")
        for fragment in (
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
        ):
            with self.subTest(fragment=fragment):
                self.assertNotIn(fragment, source)


if __name__ == "__main__":
    unittest.main()
