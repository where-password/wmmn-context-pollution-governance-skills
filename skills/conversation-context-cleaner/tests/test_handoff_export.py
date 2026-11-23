from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from conversation_cleaner.api_v1 import (  # noqa: E402
    ConversationSkillApi,
    clean_apply,
    clean_plan,
    handoff_export,
)
from conversation_cleaner.cli import main  # noqa: E402
from conversation_cleaner.contracts import CONVERSATION_SKILL_API_VERSION  # noqa: E402
from conversation_cleaner.handoff_contracts import MemoryObservation  # noqa: E402
from conversation_cleaner.handoff_export import HandoffExportInputError, _render_source_units  # noqa: E402
from conversation_cleaner.handoff_builder import build_handoff  # noqa: E402
from conversation_cleaner.state_resolution import resolve_state_transitions  # noqa: E402


CONVERSATION_ID = "conv_handoff_export_test"
WORKSPACE_ID = "ws_export"
PROJECT_ID = "project_export"
SOURCE_ID = "src_export"
SCOPE_KEY = "workspace:9:ws_export|project:14:project_export|subject:7:upgrade"
CLAIM_KEY = "claim:upgrade-state"


def _make_run(root: Path, texts: list[str], *, conversation_ids: list[str] | None = None) -> tuple[Path, str, list[dict]]:
    selected_conversations = conversation_ids or [CONVERSATION_ID]
    input_root = root / "input"
    input_root.mkdir()
    input_path = input_root / "conversation.jsonl"
    rows = []
    for sequence, text in enumerate(texts):
        conversation_id = selected_conversations[min(sequence, len(selected_conversations) - 1)]
        rows.append(
            {
                "conversation_id": conversation_id,
                "message_id": f"msg_{sequence:03d}",
                "role": "user",
                "timestamp": f"2026-11-22T10:{sequence:02d}:00+08:00",
                "sequence": sequence,
                "text": text,
                "attachments": [],
                "metadata": {"topic_key": "handoff"},
            }
        )
    input_path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    workspace_root = root / "workspace"
    plan = clean_plan(
        input_root=input_root,
        input_path=input_path,
        workspace_root=workspace_root,
        workspace_id=WORKSPACE_ID,
        project_id=PROJECT_ID,
        source_id=SOURCE_ID,
        domain_profile="generic",
        profile_version="1.0.0",
        conversation_ids=list(dict.fromkeys(selected_conversations)),
        boundary_type="explicit_scan_boundary",
        boundary_evidence="explicit handoff export test",
    )
    applied = clean_apply(plan, confirmation_token=plan["confirmation_token"])
    if applied["status"] != "applied":
        raise AssertionError(applied)
    run_id = plan["run_id"]
    run_dir = Path(applied["run_dir"])
    messages = [json.loads(line) for line in (run_dir / "messages.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    return workspace_root, run_id, messages


def _observation(
    message: dict,
    observation_id: str,
    state_kind: str,
    *,
    content: str | None = None,
    confidence: float = 0.93,
    conversation_id: str = CONVERSATION_ID,
    scope_key: str = SCOPE_KEY,
    claim_key: str = CLAIM_KEY,
) -> MemoryObservation:
    content = message["text"] if content is None else content
    evidence = {
        "evidence_id": "ev_" + observation_id,
        "source_unit_id": message["source_unit_id"],
        "quote": message["text"],
        "locator_json": {"message_id": message["message_id"], "char_start": 0, "char_end": len(message["text"])},
        "status": "verified",
    }
    return MemoryObservation.from_dict(
        {
            "observation_id": observation_id,
            "conversation_id": conversation_id,
            "source_sequence": message["sequence"],
            "observed_at": message["timestamp"],
            "claim_key": claim_key,
            "scope_key": scope_key,
            "memory_type": "constraint" if state_kind == "constraint" else "fact",
            "content": content,
            "assertion_source": "user_report",
            "verification_state": "quote_verified",
            "epistemic_status": "user_asserted",
            "state_kind": state_kind,
            "evidences": [evidence],
            "confidence": confidence,
            "effective_from": None,
            "effective_to": None,
            "review_status": "pending_review",
        }
    )


def _write_observations(root: Path, name: str, observations: list[MemoryObservation]) -> Path:
    observations_root = root / "observations"
    observations_root.mkdir(exist_ok=True)
    path = observations_root / name
    path.write_text("".join(json.dumps(item.to_dict(), ensure_ascii=False) + "\n" for item in observations), encoding="utf-8")
    return path


def _export(
    workspace_root: Path,
    run_id: str,
    observations_path: Path,
    *,
    workspace_id: str = WORKSPACE_ID,
    project_id: str = PROJECT_ID,
    domain_profile: str = "generic",
    profile_version: str = "1.0.0",
    source_id: str = SOURCE_ID,
) -> dict:
    return handoff_export(
        workspace_root=workspace_root,
        run_id=run_id,
        observations_root=observations_path.parent,
        observations_path=observations_path,
        workspace_id=workspace_id,
        project_id=project_id,
        domain_profile=domain_profile,
        profile_version=profile_version,
        source_id=source_id,
    )


def _records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class HandoffExportTests(unittest.TestCase):
    def test_new_observation_exports_artifacts_manifest_and_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace, run_id, messages = _make_run(root, ["I already upgraded."])
            observations_path = _write_observations(root, "new.jsonl", [_observation(messages[0], "obs_new", "fact")])
            result = _export(workspace, run_id, observations_path)

            self.assertEqual(result["status"], "exported")
            self.assertTrue(result["changed"])
            self.assertEqual(result["counts"]["write_items"], 1)
            self.assertEqual(result["counts"]["pending_review_items"], 0)
            self.assertEqual(CONVERSATION_SKILL_API_VERSION, "1.5.0")
            self.assertIn("handoff_export", ConversationSkillApi)
            output_dir = Path(result["output_dir"])
            self.assertTrue(output_dir.is_dir())
            for name in ("memory_write_batch.jsonl", "source_units.jsonl", "state_transitions.jsonl", "review_pending.jsonl", "handoff_manifest.json", "HANDOFF_REVIEW.md"):
                self.assertTrue((output_dir / name).is_file(), name)
            source_units = _records(output_dir / "source_units.jsonl")
            self.assertEqual(len(source_units), 1)
            source_unit = source_units[0]
            self.assertEqual(source_unit["source_unit_id"], messages[0]["source_unit_id"])
            self.assertEqual(source_unit["source_unit_hash"], messages[0]["source_unit_hash"])
            self.assertEqual(source_unit["text_content"], messages[0]["text"])
            self.assertEqual(source_unit["source_path"], f"conversation:{CONVERSATION_ID}")
            self.assertEqual(source_unit["visibility"], "private")
            self.assertEqual(source_unit["sensitivity"], "private")
            self.assertEqual(source_unit["metadata"]["message_id"], messages[0]["message_id"])

            batch = _records(output_dir / "memory_write_batch.jsonl")
            self.assertEqual(len(batch), 1)
            self.assertEqual(batch[0]["transition_kind"], "new")
            self.assertEqual(batch[0]["content"], "I already upgraded.")
            self.assertIsNone(batch[0]["supersedes_ref"])
            self.assertIsNone(batch[0]["supersedes_id"])
            markdown = (output_dir / "HANDOFF_REVIEW.md").read_text(encoding="utf-8")
            self.assertIn("Current writes", markdown)
            self.assertIn("No canonical database", markdown)

            manifest = json.loads((output_dir / "handoff_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["handoff_contract_version"], "conversation-memory-handoff-v1")
            self.assertEqual(manifest["conversation_ids"], [CONVERSATION_ID])
            self.assertEqual(manifest["run_id"], run_id)
            self.assertEqual(manifest["production_writes"], False)
            self.assertEqual(manifest["real_llm_called"], False)
            self.assertEqual(manifest["milvus_written"], False)
            self.assertEqual(manifest["alias_changed"], False)
            for name, expected_hash in manifest["artifacts"].items():
                actual = hashlib.sha256((output_dir / name).read_bytes()).hexdigest()
                self.assertEqual(actual, expected_hash)

    def test_manifest_conversation_ids_are_sorted_and_derived_from_observations(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace, run_id, messages = _make_run(
                root,
                ["z first", "z second", "a first"],
                conversation_ids=["conv_z", "conv_z", "conv_a"],
            )
            by_conversation: dict[str, list[dict]] = {}
            for message in messages:
                by_conversation.setdefault(message["conversation_id"], []).append(message)
            observations = [
                _observation(by_conversation["conv_z"][0], "obs_z1", "fact", conversation_id="conv_z"),
                _observation(by_conversation["conv_z"][1], "obs_z2", "fact", conversation_id="conv_z"),
                _observation(by_conversation["conv_a"][0], "obs_a", "fact", conversation_id="conv_a"),
            ]
            observations_path = _write_observations(root, "conversation_ids.jsonl", observations)
            result = _export(workspace, run_id, observations_path)
            if result["status"] != "exported":
                raise AssertionError(result)
            manifest = json.loads((Path(result["output_dir"]) / "handoff_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["conversation_ids"], ["conv_a", "conv_z"])
            self.assertEqual(manifest["conversation_ids"], sorted(set(manifest["conversation_ids"])))
            source_units = _records(Path(result["output_dir"]) / "source_units.jsonl")
            source_unit_ids = [unit["source_unit_id"] for unit in source_units]
            self.assertEqual(len(source_unit_ids), 3)
            self.assertEqual(source_unit_ids, sorted(set(source_unit_ids)))

    def test_supersedes_exports_only_new_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace, run_id, messages = _make_run(root, ["Do not upgrade; I have no money.", "I already upgraded."])
            observations = [_observation(messages[0], "obs_old", "constraint"), _observation(messages[1], "obs_new", "fact")]
            observations_path = _write_observations(root, "supersedes.jsonl", observations)
            result = _export(workspace, run_id, observations_path)
            output_dir = Path(result["output_dir"])
            batch = _records(output_dir / "memory_write_batch.jsonl")
            self.assertEqual(len(batch), 1)
            self.assertEqual(batch[0]["content"], "I already upgraded.")
            self.assertEqual(batch[0]["transition_kind"], "supersedes")
            self.assertEqual(batch[0]["supersedes_ref"], "obs_old")
            self.assertIsNone(batch[0]["supersedes_id"])
            self.assertEqual({item["evidence_id"] for item in batch[0]["evidences"]}, {"ev_obs_old", "ev_obs_new"})
            self.assertEqual(_records(output_dir / "review_pending.jsonl"), [])
            markdown = (output_dir / "HANDOFF_REVIEW.md").read_text(encoding="utf-8")
            self.assertIn("## Supersedes", markdown)
            self.assertEqual(result["counts"]["skipped_observations"], 1)


    def test_conflict_and_needs_review_only_go_to_pending(self) -> None:
        for later_kind, later_content in (
            ("conflict", "Sources disagree about whether I upgraded."),
            ("fact", "Maybe I upgraded, but I am not sure."),
        ):
            with self.subTest(later_kind=later_kind), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                workspace, run_id, messages = _make_run(root, ["I did not upgrade.", later_content])
                observations = [_observation(messages[0], "obs_old", "fact"), _observation(messages[1], "obs_later", later_kind)]
                observations_path = _write_observations(root, "review.jsonl", observations)
                result = _export(workspace, run_id, observations_path)
                output_dir = Path(result["output_dir"])
                batch = _records(output_dir / "memory_write_batch.jsonl")
                pending = _records(output_dir / "review_pending.jsonl")
                self.assertNotIn("obs_later", {item["domain_payload"]["memory_observation_id"] for item in batch})
                self.assertEqual(len(pending), 1)
                self.assertIn(pending[0]["transition_kind"], {"conflict", "needs_review"})
                markdown = (output_dir / "HANDOFF_REVIEW.md").read_text(encoding="utf-8")
                self.assertIn("Pending review", markdown)

    def test_unchanged_and_historical_only_are_reported_as_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace, run_id, messages = _make_run(root, ["I already upgraded.", "I already upgraded.", "Should I upgrade again?"])
            observations = [
                _observation(messages[0], "obs_old", "fact"),
                _observation(messages[1], "obs_duplicate", "fact"),
                _observation(messages[2], "obs_question", "discussion"),
            ]
            observations_path = _write_observations(root, "skipped.jsonl", observations)
            result = _export(workspace, run_id, observations_path)
            output_dir = Path(result["output_dir"])
            self.assertEqual(len(_records(output_dir / "memory_write_batch.jsonl")), 1)
            self.assertEqual(result["counts"]["skipped_observations"], 2)
            self.assertEqual(_records(output_dir / "review_pending.jsonl"), [])
            markdown = (output_dir / "HANDOFF_REVIEW.md").read_text(encoding="utf-8")
            self.assertIn("## Skipped", markdown)
            self.assertIn("unchanged", markdown)
            self.assertIn("historical_only", markdown)

    def test_duplicate_export_is_verified_noop(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace, run_id, messages = _make_run(root, ["I already upgraded."])
            observations_path = _write_observations(root, "duplicate.jsonl", [_observation(messages[0], "obs_new", "fact")])
            first = _export(workspace, run_id, observations_path)
            output_dir = Path(first["output_dir"])
            before = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in output_dir.iterdir() if path.is_file()}
            second = _export(workspace, run_id, observations_path)
            after = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in output_dir.iterdir() if path.is_file()}
            self.assertEqual(second["status"], "already_exported")
            self.assertFalse(second["changed"])
            self.assertEqual(second["handoff_id"], first["handoff_id"])
            self.assertEqual(second["counts"], first["counts"])
            self.assertEqual(before, after)

    def test_observations_or_scope_changes_create_new_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace, run_id, messages = _make_run(root, ["I already upgraded."])
            first_path = _write_observations(root, "first.jsonl", [_observation(messages[0], "obs_new", "fact", confidence=0.91)])
            first = _export(workspace, run_id, first_path)
            first_dir = Path(first["output_dir"])
            first_manifest = (first_dir / "handoff_manifest.json").read_bytes()

            changed_path = _write_observations(root, "changed.jsonl", [_observation(messages[0], "obs_new", "fact", confidence=0.92)])
            changed = _export(workspace, run_id, changed_path)
            self.assertEqual(changed["status"], "exported")
            self.assertNotEqual(changed["handoff_id"], first["handoff_id"])
            self.assertNotEqual(Path(changed["output_dir"]), first_dir)
            self.assertEqual(first_manifest, (first_dir / "handoff_manifest.json").read_bytes())

            changed_scope = _export(workspace, run_id, changed_path, workspace_id="ws_export_changed")
            self.assertEqual(changed_scope["status"], "exported")
            self.assertNotEqual(changed_scope["handoff_id"], changed["handoff_id"])

    def test_unknown_fields_and_missing_required_fields_block(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace, run_id, messages = _make_run(root, ["I already upgraded."])
            records = [_observation(messages[0], "obs_new", "fact").to_dict()]
            records[0]["unexpected_field"] = True
            observations_root = root / "observations"
            observations_root.mkdir()
            unknown_path = observations_root / "unknown.jsonl"
            unknown_path.write_text(json.dumps(records[0], ensure_ascii=False) + "\n", encoding="utf-8")
            result = _export(workspace, run_id, unknown_path)
            self.assertEqual(result["status"], "blocked")
            self.assertFalse(result["changed"])
            self.assertEqual(result["error"]["code"], "handoff_input_error")

            missing = _observation(messages[0], "obs_new", "fact").to_dict()
            missing.pop("claim_key")
            missing_path = observations_root / "missing.jsonl"
            missing_path.write_text(json.dumps(missing, ensure_ascii=False) + "\n", encoding="utf-8")
            missing_result = _export(workspace, run_id, missing_path)
            self.assertEqual(missing_result["status"], "blocked")
            self.assertFalse((workspace / "conversation-cleaner" / "v1" / "handoffs").exists())

    def test_path_escape_and_symlink_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace, run_id, messages = _make_run(root, ["I already upgraded."])
            observations_root = root / "observations"
            outside_root = root / "outside"
            observations_root.mkdir()
            outside_root.mkdir()
            valid = _write_observations(root, "valid.jsonl", [_observation(messages[0], "obs_new", "fact")])
            outside = outside_root / "outside.jsonl"
            outside.write_bytes(valid.read_bytes())
            escaped = handoff_export(
                workspace_root=workspace,
                run_id=run_id,
                observations_root=observations_root,
                observations_path=outside,
                workspace_id=WORKSPACE_ID,
                project_id=PROJECT_ID,
                domain_profile="generic",
                profile_version="1.0.0",
                source_id=SOURCE_ID,
            )
            self.assertEqual(escaped["status"], "blocked")

            symlink_path = observations_root / "link.jsonl"
            try:
                os.symlink(valid, symlink_path)
            except (OSError, NotImplementedError):
                return
            linked = _export(workspace, run_id, symlink_path)
            self.assertEqual(linked["status"], "blocked")
            self.assertEqual(linked["error"]["code"], "handoff_path_error")

    def test_secret_input_is_blocked_without_creating_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace, run_id, messages = _make_run(root, ["I already upgraded."])
            record = _observation(messages[0], "obs_secret", "fact").to_dict()
            record["content"] = "sk-ABCDEFGHIJKLMNOP"
            observations_root = root / "observations"
            observations_root.mkdir()
            secret_path = observations_root / "secret.jsonl"
            secret_path.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")
            result = _export(workspace, run_id, secret_path)
            self.assertEqual(result["status"], "blocked")
            self.assertEqual(result["error"]["code"], "handoff_input_error")
            self.assertNotIn("sk-ABCDEFGHIJKLMNOP", json.dumps(result, ensure_ascii=False))
            self.assertFalse((workspace / "conversation-cleaner" / "v1" / "handoffs").exists())

    def test_source_evidence_must_belong_to_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace, run_id, messages = _make_run(root, ["I already upgraded."])
            record = _observation(messages[0], "obs_new", "fact").to_dict()
            record["evidences"][0]["source_unit_id"] = "su_not_in_run"
            observations_root = root / "observations"
            observations_root.mkdir()
            path = observations_root / "bad_source.jsonl"
            path.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")
            result = _export(workspace, run_id, path)
            self.assertEqual(result["status"], "blocked")
            self.assertEqual(result["error"]["code"], "handoff_input_error")

    def test_source_unit_quote_hash_and_visibility_conflicts_block(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace, run_id, messages = _make_run(root, ["Exact source text for unit checks."])
            record = _observation(messages[0], "obs_quote", "fact").to_dict()
            record["evidences"][0]["quote"] = "not present in source"
            path = _write_observations(root, "bad_quote.jsonl", [MemoryObservation.from_dict(record)])
            result = _export(workspace, run_id, path)
            self.assertEqual(result["status"], "blocked")
            self.assertEqual(result["error"]["code"], "handoff_input_error")

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace, run_id, messages = _make_run(root, ["Exact source text for unit checks."])
            record = _observation(messages[0], "obs_hash", "fact").to_dict()
            record["evidences"][0]["locator_json"]["source_unit_hash"] = "wrong-hash"
            path = _write_observations(root, "bad_hash.jsonl", [MemoryObservation.from_dict(record)])
            result = _export(workspace, run_id, path)
            self.assertEqual(result["status"], "blocked")
            self.assertEqual(result["error"]["code"], "handoff_input_error")

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace, run_id, messages = _make_run(root, ["Exact source text for unit checks."])
            first = _observation(messages[0], "obs_visibility_a", "fact", claim_key="claim:a", scope_key="scope:a")
            second = _observation(messages[0], "obs_visibility_b", "fact", claim_key="claim:b", scope_key="scope:b")
            resolved = resolve_state_transitions([first, second])
            built = build_handoff(
                resolved.observations,
                resolved.transitions,
                workspace_id=WORKSPACE_ID,
                project_id=PROJECT_ID,
                domain_profile="generic",
                profile_version="1.0.0",
                source_id=SOURCE_ID,
            )
            conflicting_items = (
                replace(built.write_items[0], requested_visibility="private"),
                replace(built.write_items[1], requested_visibility="workspace"),
            )
            with self.assertRaisesRegex(HandoffExportInputError, "conflicting visibility/sensitivity"):
                _render_source_units(
                    messages_by_source_unit={messages[0]["source_unit_id"]: messages[0]},
                    write_items=conflicting_items,
                    source_id=SOURCE_ID,
                    workspace_id=WORKSPACE_ID,
                    project_id=PROJECT_ID,
                    source_sha256="a" * 64,
                )

    def test_cli_json_returns_full_path_and_counts(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace, run_id, messages = _make_run(root, ["I already upgraded."])
            observations_path = _write_observations(root, "cli.jsonl", [_observation(messages[0], "obs_new", "fact")])
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                exit_code = main([
                    "handoff", "export",
                    "--workspace-root", str(workspace),
                    "--run-id", run_id,
                    "--observations-root", str(observations_path.parent),
                    "--observations", str(observations_path),
                    "--workspace-id", WORKSPACE_ID,
                    "--project-id", PROJECT_ID,
                    "--domain-profile", "generic",
                    "--profile-version", "1.0.0",
                    "--source-id", SOURCE_ID,
                    "--json",
                ])
            self.assertEqual(exit_code, 0)
            payload = json.loads(buffer.getvalue())
            self.assertEqual(payload["status"], "exported")
            self.assertTrue(Path(payload["output_dir"]).is_absolute())
            self.assertEqual(payload["counts"]["write_items"], 1)
            self.assertIn("handoff_id", payload)

    def test_tampered_artifact_blocks_duplicate_export(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace, run_id, messages = _make_run(root, ["I already upgraded."])
            observations_path = _write_observations(root, "tamper.jsonl", [_observation(messages[0], "obs_new", "fact")])
            first = _export(workspace, run_id, observations_path)
            output_dir = Path(first["output_dir"])
            tampered = output_dir / "review_pending.jsonl"
            tampered.write_text("tampered\n", encoding="utf-8")
            second = _export(workspace, run_id, observations_path)
            self.assertEqual(second["status"], "blocked")
            self.assertEqual(second["error"]["code"], "handoff_export_conflict")
            self.assertEqual(tampered.read_text(encoding="utf-8"), "tampered\n")

    def test_no_production_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace, run_id, messages = _make_run(root, ["I already upgraded."])
            production = workspace / "production"
            production.mkdir()
            sentinel = production / "canonical.sqlite3"
            sentinel.write_bytes(b"production-placeholder")
            before = hashlib.sha256(sentinel.read_bytes()).hexdigest()
            observations_path = _write_observations(root, "safe.jsonl", [_observation(messages[0], "obs_new", "fact")])
            result = _export(workspace, run_id, observations_path)
            self.assertEqual(result["status"], "exported")
            self.assertEqual(hashlib.sha256(sentinel.read_bytes()).hexdigest(), before)
            self.assertFalse((workspace / "production" / "milvus").exists())

    def test_exporter_has_no_network_llm_or_environment_calls(self) -> None:
        source = (SCRIPTS / "conversation_cleaner" / "handoff_export.py").read_text(encoding="utf-8")
        for fragment in ("requests", "httpx", "urllib", "socket", "openai", "pymilvus", "os.environ", "os.getenv", "os.walk", "rglob(", "glob("):
            with self.subTest(fragment=fragment):
                self.assertNotIn(fragment, source)


if __name__ == "__main__":
    unittest.main()
