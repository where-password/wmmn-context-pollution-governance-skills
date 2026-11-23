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

from conversation_cleaner.api_v1 import clean_apply, clean_plan, review_export, status  # noqa: E402
from conversation_cleaner.handoff_contracts import MemoryObservation  # noqa: E402


CONVERSATION_ID = "conv_observation_pipeline"
WORKSPACE_ID = "ws_observation_pipeline"
PROJECT_ID = "project_observation_pipeline"
SOURCE_ID = "src_observation_pipeline"
SCOPE_KEY = "workspace:ws_observation_pipeline|project:project_observation_pipeline|subject:memory"


def _records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _row(sequence: int, text: str, *, observation: dict | None = None) -> dict:
    metadata = {"topic_key": "memory-upgrade"}
    if observation is not None:
        metadata["observation"] = observation
    return {
        "conversation_id": CONVERSATION_ID,
        "message_id": f"m{sequence:03d}",
        "role": "user",
        "timestamp": f"2026-11-22T10:{sequence:02d}:00+08:00",
        "sequence": sequence,
        "text": text,
        "attachments": [],
        "metadata": metadata,
    }


def _plan(root: Path, rows: list[dict], *, slug: str) -> tuple[dict, Path]:
    input_root = root / f"input_{slug}"
    input_root.mkdir()
    input_path = input_root / "conversation.jsonl"
    input_path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    workspace = root / f"workspace_{slug}"
    plan = clean_plan(
        input_root=input_root,
        input_path=input_path,
        workspace_root=workspace,
        workspace_id=WORKSPACE_ID,
        project_id=PROJECT_ID,
        source_id=SOURCE_ID,
        domain_profile="generic",
        profile_version="1.0.0",
        conversation_ids=[CONVERSATION_ID],
        boundary_type="conversation_closed",
        boundary_evidence="explicit observation pipeline test",
    )
    return plan, workspace


class ObservationPipelineTests(unittest.TestCase):
    def test_explicit_claim_metadata_produces_supersedes_artifacts(self) -> None:
        rows = [
            _row(
                0,
                "DECISION: 暂时不升级内存，因为没有预算。",
                observation={"claim_key": "claim:memory-upgrade", "scope_key": SCOPE_KEY, "state_kind": "constraint"},
            ),
            _row(
                1,
                "FACT: 已经升级到 32GB 内存。",
                observation={"claim_key": "claim:memory-upgrade", "scope_key": SCOPE_KEY, "state_kind": "fact"},
            ),
        ]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            plan, workspace = _plan(root, rows, slug="upgrade")
            self.assertEqual(plan["status"], "planned")
            self.assertEqual(plan["counts"]["observations"], 2)
            self.assertEqual(plan["counts"]["state_transitions"], 2)
            self.assertEqual(plan["counts"]["pending_state_reviews"], 0)
            self.assertEqual(plan["counts"]["observation_generation_issues"], 0)
            self.assertFalse(workspace.exists(), "clean plan must remain read-only")

            applied = clean_apply(plan, confirmation_token=plan["confirmation_token"])
            self.assertEqual(applied["status"], "applied")
            run_dir = Path(applied["run_dir"])
            for name in ("observations.jsonl", "state_transitions.jsonl", "state_review.jsonl"):
                self.assertTrue((run_dir / name).is_file(), name)

            observations = _records(run_dir / "observations.jsonl")
            transitions = _records(run_dir / "state_transitions.jsonl")
            state_reviews = _records(run_dir / "state_review.jsonl")
            self.assertEqual(len(observations), 2)
            for observation in observations:
                MemoryObservation.from_dict(observation)
            supersedes = [item for item in transitions if item["transition_kind"] == "supersedes"]
            self.assertEqual(len(supersedes), 1)
            self.assertEqual(supersedes[0]["from_observation_id"], observations[0]["observation_id"])
            self.assertEqual(supersedes[0]["to_observation_id"], observations[1]["observation_id"])
            self.assertEqual(state_reviews, [])
            self.assertFalse((workspace / "conversation-cleaner" / "v1" / "handoffs").exists())

            stats = json.loads((run_dir / "review_stats.json").read_text(encoding="utf-8"))
            audit = json.loads((run_dir / "audit_report.json").read_text(encoding="utf-8"))
            self.assertEqual(stats["observations"], 2)
            self.assertEqual(stats["state_transitions"], 2)
            self.assertEqual(stats["pending_state_reviews"], 0)
            self.assertEqual(stats["observation_generation_issues"], 0)
            self.assertEqual(audit["counts"]["observations"], 2)
            self.assertEqual(audit["counts"]["state_transitions"], 2)
            self.assertEqual(audit["counts"]["pending_state_reviews"], 0)
            markdown = (run_dir / "REVIEW_BUNDLE.md").read_text(encoding="utf-8")
            self.assertIn("| observations | 2 |", markdown)
            self.assertIn("| state transitions | 2 |", markdown)

            manifest = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["artifact_count"], 16)
            for name in ("observations.jsonl", "state_transitions.jsonl", "state_review.jsonl"):
                self.assertIn(name, manifest["artifacts"])
                actual = hashlib.sha256((run_dir / name).read_bytes()).hexdigest()
                self.assertEqual(actual, manifest["artifacts"][name])

            export = review_export(workspace_root=workspace, run_id=applied["run_id"])
            export_dir = Path(export["output_dir"])
            for name in ("observations.jsonl", "state_transitions.jsonl", "state_review.jsonl"):
                self.assertTrue((export_dir / name).is_file(), name)
            export_manifest = json.loads((export_dir / "export_manifest.json").read_text(encoding="utf-8"))
            self.assertTrue({"observations.jsonl", "state_transitions.jsonl", "state_review.jsonl"} <= set(export_manifest["artifacts"]))

            before = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in run_dir.iterdir() if path.is_file()}
            repeated = clean_apply(plan, confirmation_token=plan["confirmation_token"])
            self.assertEqual(repeated["status"], "already_applied")
            self.assertFalse(repeated["changed"])
            after = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in run_dir.iterdir() if path.is_file()}
            self.assertEqual(before, after)
            export_again = review_export(workspace_root=workspace, run_id=applied["run_id"])
            self.assertEqual(export_again["status"], "already_exported")
            self.assertFalse(export_again["changed"])
            self.assertEqual(status(workspace_root=workspace)["status"], "ok")

    def test_conflict_observation_is_written_to_state_review_artifact(self) -> None:
        rows = [
            _row(
                0,
                "FACT: I did not upgrade.",
                observation={"claim_key": "claim:conflict-upgrade", "scope_key": SCOPE_KEY, "state_kind": "fact"},
            ),
            _row(
                1,
                "FACT: Sources disagree about my upgrade state.",
                observation={"claim_key": "claim:conflict-upgrade", "scope_key": SCOPE_KEY, "state_kind": "conflict"},
            ),
        ]
        with tempfile.TemporaryDirectory() as temp:
            plan, _ = _plan(Path(temp), rows, slug="state_review")
            self.assertEqual(plan["status"], "planned")
            self.assertEqual(plan["counts"]["pending_state_reviews"], 1)
            applied = clean_apply(plan, confirmation_token=plan["confirmation_token"])
            run_dir = Path(applied["run_dir"])
            state_reviews = _records(run_dir / "state_review.jsonl")
            self.assertEqual(len(state_reviews), 1)
            self.assertEqual(state_reviews[0]["transition_kind"], "conflict")
            stats = json.loads((run_dir / "review_stats.json").read_text(encoding="utf-8"))
            self.assertEqual(stats["pending_state_reviews"], 1)

    def test_no_metadata_creates_independent_observations_without_false_supersede(self) -> None:
        rows = [
            _row(0, "DECISION: I did not upgrade."),
            _row(1, "FACT: I already upgraded."),
        ]
        with tempfile.TemporaryDirectory() as temp:
            plan, _ = _plan(Path(temp), rows, slug="fallback")
            self.assertEqual(plan["status"], "planned")
            self.assertEqual(plan["counts"]["observations"], 2)
            self.assertGreaterEqual(plan["counts"]["state_transitions"], 2)
            applied = clean_apply(plan, confirmation_token=plan["confirmation_token"])
            run_dir = Path(applied["run_dir"])
            transitions = _records(run_dir / "state_transitions.jsonl")
            self.assertFalse(any(item["transition_kind"] == "supersedes" for item in transitions))

    def test_invalid_metadata_blocks_plan_without_workspace_or_body_echo(self) -> None:
        secretish_body = "DECISION: keep this controlled body private."
        rows = [_row(0, secretish_body, observation={"claim_key": "claim:x", "unknown": True})]
        with tempfile.TemporaryDirectory() as temp:
            plan, workspace = _plan(Path(temp), rows, slug="invalid")
            self.assertEqual(plan["status"], "blocked")
            self.assertEqual(plan["counts"]["observation_generation_issues"], 1)
            self.assertFalse(workspace.exists())
            encoded = json.dumps(plan, ensure_ascii=False)
            self.assertNotIn(secretish_body, encoded)
            self.assertIn("observation_generation_blocked", {item["code"] for item in plan["errors"]})

    def test_ambiguous_metadata_blocks_plan_without_workspace(self) -> None:
        text = "* DECISION: keep alpha.\n* DECISION: keep beta."
        rows = [_row(0, text, observation={"claim_key": "claim:too-broad"})]
        with tempfile.TemporaryDirectory() as temp:
            plan, workspace = _plan(Path(temp), rows, slug="ambiguous")
            self.assertEqual(plan["status"], "blocked")
            self.assertEqual(plan["counts"]["observation_generation_issues"], 1)
            self.assertFalse(workspace.exists())
            self.assertIn("ambiguous_observation_metadata", {item["code"] for item in plan["errors"]})
            self.assertNotIn(text, json.dumps(plan, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
