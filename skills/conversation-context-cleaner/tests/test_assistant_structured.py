from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from conversation_cleaner.api_v1 import clean_apply, clean_plan  # noqa: E402
from conversation_cleaner.handoff_contracts import MemoryObservation  # noqa: E402


CONVERSATION_ID = "conv_structured_test"
WORKSPACE_ID = "ws_structured_test"
PROJECT_ID = "project_structured_test"
SCOPE_KEY = "workspace:ws_structured_test|project:project_structured_test|subject:cache"


def _records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _row(sequence: int, role: str, text: str, *, conversation_id: str = CONVERSATION_ID) -> dict:
    return {
        "conversation_id": conversation_id,
        "message_id": f"m{sequence:03d}",
        "role": role,
        "timestamp": f"2026-11-23T10:{sequence:02d}:00+08:00",
        "sequence": sequence,
        "text": text,
        "attachments": [],
        "metadata": {"topic_key": "structured"},
    }


def _claim(**overrides) -> dict:
    claim = {
        "claim_key": "claim:cache-location",
        "scope_key": SCOPE_KEY,
        "state_kind": "decision",
        "content": "The cache remains local.",
        "assertion_source": "user_decision",
        "verification_state": "quote_verified",
        "importance": "high",
        "citations": [{"message_id": "m000", "quote": "The cache is local.", "role": "user"}],
    }
    claim.update(overrides)
    return claim


def _block(claims: list[dict], *, contract_version: str = "conversation-memory-candidates-v1") -> str:
    return "```memory-candidates-v1\n" + json.dumps(
        {"contract_version": contract_version, "claims": claims},
        ensure_ascii=False,
    ) + "\n```"


def _plan(root: Path, rows: list[dict], *, conversation_ids: list[str] | None = None) -> tuple[dict, Path]:
    input_root = root / "input"
    input_root.mkdir(parents=True)
    input_path = input_root / "structured.jsonl"
    input_path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    workspace = root / "workspace"
    plan = clean_plan(
        input_root=input_root,
        input_path=input_path,
        workspace_root=workspace,
        workspace_id=WORKSPACE_ID,
        project_id=PROJECT_ID,
        source_id="src_structured_test",
        domain_profile="generic",
        profile_version="1.0.0",
        conversation_ids=conversation_ids or [CONVERSATION_ID],
        boundary_type="explicit_scan_boundary",
        boundary_evidence="structured assistant candidate test",
    )
    return plan, workspace


def _apply(plan: dict) -> dict:
    applied = clean_apply(plan, confirmation_token=plan["confirmation_token"])
    if applied["status"] != "applied":
        raise AssertionError(applied)
    return applied


class AssistantStructuredTests(unittest.TestCase):
    def test_valid_structured_block_generates_candidates_and_observations(self) -> None:
        rows = [
            _row(0, "user", "The cache is local."),
            _row(1, "assistant", "Here is the normalized result.\n" + _block([_claim()])),
        ]
        with tempfile.TemporaryDirectory() as temp:
            plan, _ = _plan(Path(temp), rows)
            self.assertEqual(plan["status"], "planned")
            self.assertEqual(plan["extractor_summary"]["explicit_candidate_count"], 1)
            applied = _apply(plan)
            run_dir = Path(applied["run_dir"])
            candidates = _records(run_dir / "raw_candidates.jsonl")
            self.assertEqual(len(candidates), 1)
            candidate = candidates[0]
            self.assertEqual(candidate["extraction_method"], "assistant_structured")
            self.assertEqual(candidate["verification_state"], "quote_verified")
            self.assertEqual(candidate["assertion_source"], "user_decision")
            self.assertEqual(candidate["claim_key"], "claim:cache-location")
            self.assertEqual(candidate["scope_key"], SCOPE_KEY)
            self.assertEqual(candidate["state_kind"], "decision")
            self.assertEqual(candidate["evidence"][0]["message_id"], "m000")
            self.assertEqual(candidate["evidence"][0]["char_start"], 0)
            observation_records = _records(run_dir / "observations.jsonl")
            observation = MemoryObservation.from_dict(observation_records[0])
            self.assertEqual(observation.claim_key, "claim:cache-location")
            self.assertEqual(observation.scope_key, SCOPE_KEY)
            self.assertEqual(observation.state_kind, "decision")
            self.assertEqual(observation.content, "The cache remains local.")

    def test_user_decision_requires_user_citation_and_can_use_multiple_claims(self) -> None:
        rows = [
            _row(0, "user", "The cache is local."),
            _row(
                1,
                "assistant",
                _block([
                    _claim(),
                    _claim(
                        claim_key="claim:audit-record",
                        state_kind="constraint",
                        content="Keep the audit record.",
                        assertion_source="user_request",
                        citations=[{"message_id": "m000", "quote": "The cache is local.", "role": "user"}],
                    ),
                ]),
            ),
        ]
        with tempfile.TemporaryDirectory() as temp:
            plan, _ = _plan(Path(temp), rows)
            self.assertEqual(plan["status"], "planned")
            applied = _apply(plan)
            run_dir = Path(applied["run_dir"])
            observations = [MemoryObservation.from_dict(item) for item in _records(run_dir / "observations.jsonl")]
            self.assertEqual({item.claim_key for item in observations}, {"claim:cache-location", "claim:audit-record"})
            self.assertEqual({item.assertion_source for item in observations}, {"user_decision", "user_request"})

    def test_ai_inference_is_downgraded_and_requires_review(self) -> None:
        rows = [
            _row(
                0,
                "assistant",
                "Assistant inference is under review.\n" + _block([
                    _claim(
                        state_kind="fact",
                        content="The cache is likely local.",
                        assertion_source="assistant_inference",
                        verification_state="tool_verified",
                        citations=[{"message_id": "m000", "quote": "Assistant inference is under review.", "role": "assistant"}],
                    )
                ]),
            ),
        ]
        with tempfile.TemporaryDirectory() as temp:
            plan, _ = _plan(Path(temp), rows)
            self.assertEqual(plan["status"], "planned")
            applied = _apply(plan)
            run_dir = Path(applied["run_dir"])
            candidate = _records(run_dir / "raw_candidates.jsonl")[0]
            self.assertEqual(candidate["verification_state"], "unverified")
            self.assertIn("verification_downgraded", candidate["risk_flags"])
            self.assertIn("assistant_inference_requires_review", candidate["risk_flags"])
            observation = MemoryObservation.from_dict(_records(run_dir / "observations.jsonl")[0])
            self.assertEqual(observation.verification_state, "unverified")

    def test_assistant_summary_alias_is_review_only(self) -> None:
        rows = [
            _row(
                0,
                "assistant",
                "Assistant summary is under review.\n" + _block([
                    _claim(
                        state_kind="fact",
                        content="Summary claim.",
                        assertion_source="assistant_summary",
                        verification_state="quote_verified",
                        citations=[{"message_id": "m000", "quote": "Assistant summary is under review.", "role": "assistant"}],
                    )
                ]),
            ),
        ]
        with tempfile.TemporaryDirectory() as temp:
            plan, _ = _plan(Path(temp), rows)
            self.assertEqual(plan["status"], "planned")
            applied = _apply(plan)
            candidate = _records(Path(applied["run_dir"]) / "raw_candidates.jsonl")[0]
            self.assertEqual(candidate["assertion_source"], "assistant_inference")
            self.assertIn("assistant_summary_requires_review", candidate["risk_flags"])
            self.assertEqual(candidate["review_status"], "pending_review")

    def test_tool_citation_can_support_tool_verified(self) -> None:
        rows = [
            _row(0, "user", "Tool result: cache local."),
            _row(
                1,
                "assistant",
                _block([
                    _claim(
                        state_kind="fact",
                        content="The cache is local.",
                        assertion_source="tool_output",
                        verification_state="tool_verified",
                        citations=[{"message_id": "m000", "quote": "Tool result: cache local.", "role": "tool"}],
                    )
                ]),
            ),
        ]
        with tempfile.TemporaryDirectory() as temp:
            plan, _ = _plan(Path(temp), rows)
            self.assertEqual(plan["status"], "planned")
            applied = _apply(plan)
            candidate = _records(Path(applied["run_dir"]) / "raw_candidates.jsonl")[0]
            self.assertEqual(candidate["verification_state"], "tool_verified")
            self.assertNotIn("verification_downgraded", candidate["risk_flags"])

    def test_invalid_citation_and_unknown_fields_block_plan_without_echo(self) -> None:
        cases = [
            (_claim(citations=[{"message_id": "missing", "quote": "The cache is local."}]), "citation.message_id does not exist"),
            (_claim(citations=[{"message_id": "m000", "quote": "not in source"}]), "citation.quote is not an exact substring"),
            (_claim(unknown_field=True), "unknown fields"),
            (_claim(state_kind="not-a-state"), "state_kind is invalid"),
            (_claim(citations=[]), "citations must be a non-empty array"),
        ]
        for claim, expected in cases:
            with self.subTest(expected=expected):
                with tempfile.TemporaryDirectory() as temp:
                    rows = [_row(0, "user", "The cache is local."), _row(1, "assistant", _block([claim]))]
                    plan, workspace = _plan(Path(temp), rows)
                    self.assertEqual(plan["status"], "blocked")
                    self.assertFalse(workspace.exists())
                    encoded = json.dumps(plan, ensure_ascii=False)
                    self.assertNotIn("The cache remains local.", encoded)
                    self.assertIn("assistant_structured", encoded)

    def test_multiple_blocks_and_cross_conversation_block(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            text = _block([_claim()]) + "\n" + _block([_claim()])
            rows = [_row(0, "user", "The cache is local."), _row(1, "assistant", text)]
            plan, _ = _plan(Path(temp), rows)
            self.assertEqual(plan["status"], "blocked")
            self.assertIn("assistant_structured", json.dumps(plan, ensure_ascii=False))

        with tempfile.TemporaryDirectory() as temp:
            rows = [
                _row(0, "user", "The cache is local.", conversation_id="conv_other"),
                _row(1, "assistant", _block([_claim(citations=[{"message_id": "m000", "quote": "The cache is local."}])])),
            ]
            plan, _ = _plan(Path(temp), rows, conversation_ids=[CONVERSATION_ID, "conv_other"])
            self.assertEqual(plan["status"], "blocked")
            self.assertIn("another conversation", json.dumps(plan, ensure_ascii=False))

    def test_forged_locator_is_ignored_and_actual_span_is_computed(self) -> None:
        rows = [
            _row(0, "user", "Prefix text. The cache is local."),
            _row(
                1,
                "assistant",
                _block([
                    _claim(citations=[{
                        "message_id": "m000",
                        "quote": "The cache is local.",
                        "locator_json": {"char_start": 999, "char_end": 1000},
                    }])
                ]),
            ),
        ]
        with tempfile.TemporaryDirectory() as temp:
            plan, _ = _plan(Path(temp), rows)
            self.assertEqual(plan["status"], "planned")
            applied = _apply(plan)
            candidate = _records(Path(applied["run_dir"]) / "raw_candidates.jsonl")[0]
            evidence = candidate["evidence"][0]
            self.assertEqual((evidence["char_start"], evidence["char_end"]), (13, 32))

    def test_multiple_citations_preserve_all_real_sources(self) -> None:
        rows = [
            _row(0, "user", "The cache is local."),
            _row(
                1,
                "assistant",
                "Evidence note.\n" + _block([
                    _claim(
                        state_kind="fact",
                        assertion_source="user_report",
                        verification_state="quote_verified",
                        citations=[
                            {"message_id": "m000", "quote": "The cache is local.", "role": "user"},
                            {"message_id": "m001", "quote": "Evidence note.", "role": "assistant"},
                        ],
                    )
                ]),
            ),
        ]
        with tempfile.TemporaryDirectory() as temp:
            plan, _ = _plan(Path(temp), rows)
            self.assertEqual(plan["status"], "planned")
            applied = _apply(plan)
            run_dir = Path(applied["run_dir"])
            candidate = _records(run_dir / "raw_candidates.jsonl")[0]
            self.assertEqual(len(candidate["evidence"]), 2)
            observation = MemoryObservation.from_dict(_records(run_dir / "observations.jsonl")[0])
            self.assertEqual({item.quote for item in observation.evidences}, {"The cache is local.", "Evidence note."})
            self.assertEqual({item.locator_json["message_id"] for item in observation.evidences}, {"m000", "m001"})

    def test_repeat_extraction_is_idempotent_and_no_network_or_file_discovery(self) -> None:
        rows = [_row(0, "user", "The cache is local."), _row(1, "assistant", _block([_claim()]))]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first, _ = _plan(root / "first", rows)
            second, _ = _plan(root / "second", rows)
            self.assertEqual(first["extraction_digest"], second["extraction_digest"])
            self.assertEqual(first["counts"], second["counts"])
            source = (SCRIPTS / "conversation_cleaner" / "assistant_structured.py").read_text(encoding="utf-8")
            self.assertNotIn("requests", source)
            self.assertNotIn("httpx", source)
            self.assertNotIn("open(", source)


if __name__ == "__main__":
    unittest.main()
