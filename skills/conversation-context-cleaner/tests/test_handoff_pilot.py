from __future__ import annotations

import contextlib
import hashlib
import io
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
from conversation_cleaner.cli import main  # noqa: E402
from conversation_cleaner.handoff_contracts import MemoryObservation  # noqa: E402
from conversation_cleaner.state_resolution import build_scope_key  # noqa: E402


FIXTURE_ROOT = SKILL_ROOT / "assets" / "synthetic"
FIXTURE = FIXTURE_ROOT / "handoff_state_transition_conversation.jsonl"
CONVERSATION_ID = "conv_synthetic_handoff_upgrade_001"
WORKSPACE_ID = "ws_handoff_pilot"
PROJECT_ID = "project_handoff_pilot"
SOURCE_ID = "src_handoff_pilot"
DOMAIN_PROFILE = "generic"
PROFILE_VERSION = "1.0.0"
CLAIM_KEY = "claim:device-memory-upgrade"
SCOPE_KEY = build_scope_key(workspace_id=WORKSPACE_ID, project_id=PROJECT_ID, subject="device-memory")


def _records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _observation(message: dict, observation_id: str, state_kind: str) -> MemoryObservation:
    quote = message["text"]
    return MemoryObservation.from_dict(
        {
            "observation_id": observation_id,
            "conversation_id": message["conversation_id"],
            "source_sequence": message["sequence"],
            "observed_at": message["timestamp"],
            "claim_key": CLAIM_KEY,
            "scope_key": SCOPE_KEY,
            "memory_type": "constraint" if state_kind == "constraint" else "fact",
            "content": quote,
            "assertion_source": "user_report",
            "verification_state": "quote_verified",
            "epistemic_status": "user_asserted",
            "state_kind": state_kind,
            "evidences": [
                {
                    "evidence_id": "ev_" + observation_id,
                    "source_unit_id": message["source_unit_id"],
                    "quote": quote,
                    "locator_json": {"message_id": message["message_id"], "char_start": 0, "char_end": len(quote)},
                    "status": "verified",
                }
            ],
            "confidence": 0.95,
            "effective_from": None,
            "effective_to": None,
            "review_status": "pending_review",
        }
    )


def _run_clean(root: Path) -> tuple[Path, str, list[dict]]:
    workspace = root / "workspace"
    plan = clean_plan(
        input_root=FIXTURE_ROOT,
        input_path=FIXTURE,
        workspace_root=workspace,
        workspace_id=WORKSPACE_ID,
        project_id=PROJECT_ID,
        source_id=SOURCE_ID,
        domain_profile=DOMAIN_PROFILE,
        profile_version=PROFILE_VERSION,
        conversation_ids=[CONVERSATION_ID],
        boundary_type="conversation_closed",
        boundary_evidence="synthetic handoff upgrade pilot fixture",
    )
    applied = clean_apply(plan, confirmation_token=plan["confirmation_token"])
    if applied["status"] != "applied":
        raise AssertionError(applied)
    return workspace, plan["run_id"], _records(Path(applied["run_dir"]) / "messages.jsonl")


def _write_observations(root: Path, observations: list[MemoryObservation]) -> Path:
    observations_root = root / "observations"
    observations_root.mkdir()
    path = observations_root / "memory_upgrade_observations.jsonl"
    path.write_text("".join(json.dumps(item.to_dict(), ensure_ascii=False) + "\n" for item in observations), encoding="utf-8")
    return path


def _cli_export(workspace: Path, run_id: str, observations_path: Path) -> tuple[int, dict]:
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        code = main([
            "handoff", "export",
            "--workspace-root", str(workspace),
            "--run-id", run_id,
            "--observations-root", str(observations_path.parent),
            "--observations", str(observations_path),
            "--workspace-id", WORKSPACE_ID,
            "--project-id", PROJECT_ID,
            "--domain-profile", DOMAIN_PROFILE,
            "--profile-version", PROFILE_VERSION,
            "--source-id", SOURCE_ID,
            "--json",
        ])
    return code, json.loads(output.getvalue())


def _artifact_hashes(output_dir: Path) -> dict[str, str]:
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(output_dir.iterdir())
        if path.is_file()
    }


def _section(markdown: str, heading: str, next_heading: str) -> str:
    start = markdown.index(heading)
    end = markdown.index(next_heading, start + len(heading))
    return markdown[start:end]


class HandoffUpgradePilotTests(unittest.TestCase):
    def test_fixture_has_one_explicit_upgrade_conversation(self) -> None:
        rows = _records(FIXTURE)
        self.assertEqual(len(rows), 3)
        self.assertEqual({row["conversation_id"] for row in rows}, {CONVERSATION_ID})
        self.assertEqual([row["sequence"] for row in rows], [0, 1, 2])
        self.assertEqual([row["role"] for row in rows], ["user", "assistant", "user"])
        self.assertIn("暂时不升级内存", rows[0]["text"])
        self.assertIn("32GB", rows[2]["text"])

    def test_full_upgrade_transition_pilot_and_duplicate_export(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace, run_id, messages = _run_clean(root)
            user_messages = [message for message in messages if message["role"] == "user"]
            self.assertEqual(len(user_messages), 2)

            old = _observation(user_messages[0], "obs_memory_not_upgraded", "constraint")
            new = _observation(user_messages[1], "obs_memory_upgraded_32gb", "fact")
            observations_path = _write_observations(root, [old, new])

            code, first = _cli_export(workspace, run_id, observations_path)
            self.assertEqual(code, 0)
            self.assertEqual(first["status"], "exported")
            self.assertTrue(first["changed"])
            output_dir = Path(first["output_dir"])
            self.assertEqual(output_dir.resolve().parent, (workspace / "conversation-cleaner" / "v1" / "handoffs").resolve())

            write_items = _records(output_dir / "memory_write_batch.jsonl")
            source_units = _records(output_dir / "source_units.jsonl")
            transitions = _records(output_dir / "state_transitions.jsonl")
            pending = _records(output_dir / "review_pending.jsonl")
            self.assertEqual(len(write_items), 1)
            self.assertNotIn("暂时不升级内存", write_items[0]["content"])
            self.assertIn("32GB", write_items[0]["content"])
            self.assertEqual(write_items[0]["transition_kind"], "supersedes")
            self.assertEqual(write_items[0]["supersedes_ref"], old.observation_id)
            self.assertIsNone(write_items[0]["supersedes_id"])
            self.assertEqual({item["evidence_id"] for item in write_items[0]["evidences"]}, {"ev_obs_memory_not_upgraded", "ev_obs_memory_upgraded_32gb"})

            supersedes = [item for item in transitions if item["transition_kind"] == "supersedes"]
            self.assertEqual(len(supersedes), 1)
            self.assertEqual(supersedes[0]["from_observation_id"], old.observation_id)
            self.assertEqual(supersedes[0]["to_observation_id"], new.observation_id)
            self.assertEqual(pending, [])

            old_transition = next(item for item in transitions if item["to_observation_id"] == old.observation_id)
            self.assertIn(old_transition["transition_kind"], {"new", "historical_only", "unchanged"})
            review = (output_dir / "HANDOFF_REVIEW.md").read_text(encoding="utf-8")
            current_section = _section(review, "## Current writes", "## Supersedes")
            supersedes_section = _section(review, "## Supersedes", "## Pending review")
            skipped_section = _section(review, "## Skipped", "## Database boundary")
            self.assertNotIn("暂时不升级内存", current_section)
            self.assertIn(old.observation_id, supersedes_section)
            self.assertIn("32GB", supersedes_section)
            self.assertIn(old.observation_id, skipped_section)
            self.assertIn("superseded", skipped_section)
            self.assertIn("No canonical database", review)
            self.assertIn("not proof of persistence", review)

            manifest = json.loads((output_dir / "handoff_manifest.json").read_text(encoding="utf-8"))
            for field in ("production_writes", "real_llm_called", "milvus_written", "alias_changed"):
                self.assertIs(manifest[field], False)
            self.assertEqual(len(source_units), 2)
            self.assertEqual(
                [unit["source_unit_id"] for unit in source_units],
                sorted(unit["source_unit_id"] for unit in source_units),
            )
            self.assertEqual({unit["message_id"] for unit in source_units}, {message["message_id"] for message in user_messages})
            self.assertIn("source_units.jsonl", manifest["artifacts"])
            self.assertEqual(manifest["counts"]["source_units"], 2)
            self.assertIn("- source units: `2`", review)
            self.assertEqual(manifest["counts"]["write_items"], 1)
            self.assertEqual(manifest["counts"]["pending_review_items"], 0)
            self.assertEqual(manifest["counts"]["superseding_writes"], 1)

            before = _artifact_hashes(output_dir)
            code_again, second = _cli_export(workspace, run_id, observations_path)
            after = _artifact_hashes(output_dir)
            self.assertEqual(code_again, 0)
            self.assertEqual(second["status"], "already_exported")
            self.assertIs(second["changed"], False)
            self.assertEqual(second["handoff_id"], first["handoff_id"])
            self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
