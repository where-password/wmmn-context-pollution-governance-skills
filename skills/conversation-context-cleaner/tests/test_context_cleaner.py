from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from conversation_cleaner.api_v1 import clean_apply, clean_plan, doctor, review_export, status
from conversation_cleaner.contracts import Scope
from conversation_cleaner.extraction import extract_candidates
from conversation_cleaner.parsing import parse_controlled_jsonl
from conversation_cleaner.segmentation import segment_messages
from conversation_cleaner.security import scan_text_for_secrets
from conversation_cleaner.validation import validate_candidate_evidence, validate_evidence_span


FIXTURE_ROOT = SKILL_ROOT / "assets" / "synthetic"
FIXTURE = FIXTURE_ROOT / "controlled_conversation.jsonl"
CONVERSATION_ID = "conv_synthetic_001"


def _records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _hash_tree(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    if not root.exists():
        return result
    for path in sorted(root.rglob("*")):
        if path.is_file():
            result[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def _plan(workspace_root: Path, input_root: Path = FIXTURE_ROOT, input_path: Path = FIXTURE) -> dict:
    return clean_plan(
        input_root=input_root,
        input_path=input_path,
        workspace_root=workspace_root,
        workspace_id="ws_synthetic",
        project_id="project_synthetic",
        source_id="src_synthetic",
        domain_profile="generic",
        profile_version="1.0.0",
        conversation_ids=[CONVERSATION_ID],
        boundary_type="explicit_scan_boundary",
        boundary_evidence="explicit synthetic acceptance run",
    )


def _apply(workspace_root: Path) -> tuple[dict, dict]:
    plan = _plan(workspace_root)
    applied = clean_apply(plan, confirmation_token=plan["confirmation_token"])
    return plan, applied


def _message(sequence: int, *, role: str = "user", text: str | None = None, metadata: dict | None = None) -> dict:
    return {
        "conversation_id": CONVERSATION_ID,
        "message_id": f"m{sequence:03d}",
        "role": role,
        "timestamp": f"2026-11-21T10:{sequence:02d}:00+08:00",
        "sequence": sequence,
        "text": text or f"message {sequence}",
        "attachments": [],
        "metadata": metadata or {},
    }


class ConversationCleanerTests(unittest.TestCase):
    def test_plan_is_read_only_and_duplicate_apply_is_noop(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp) / "workspace"
            plan_one = _plan(workspace)
            plan_two = _plan(workspace)
            self.assertEqual(plan_one["status"], "planned")
            self.assertEqual(plan_one["plan_id"], plan_two["plan_id"])
            self.assertEqual(plan_one["input"]["sha256"], plan_two["input"]["sha256"])
            self.assertFalse(workspace.exists(), "clean plan must not create workspace state")

            applied = clean_apply(plan_one, confirmation_token=plan_one["confirmation_token"])
            self.assertEqual(applied["status"], "applied")
            run_dir = Path(applied["run_dir"])
            first_hashes = _hash_tree(run_dir)
            self.assertIn("run_manifest.json", first_hashes)
            self.assertIn("audit_report.json", first_hashes)
            self.assertIn("observations.jsonl", first_hashes)
            self.assertIn("state_transitions.jsonl", first_hashes)
            self.assertIn("state_review.jsonl", first_hashes)
            manifest = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["artifact_count"], 16)
            self.assertTrue(
                {"observations.jsonl", "state_transitions.jsonl", "state_review.jsonl"}
                <= set(manifest["artifacts"])
            )

            applied_again = clean_apply(plan_one, confirmation_token=plan_one["confirmation_token"])
            self.assertEqual(applied_again["status"], "already_applied")
            self.assertFalse(applied_again["changed"])
            self.assertEqual(first_hashes, _hash_tree(run_dir))

            status_result = status(workspace_root=workspace)
            self.assertEqual(status_result["status"], "ok")
            self.assertEqual(status_result["run_count"], 1)

    def test_topic_candidate_evidence_and_open_question(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp) / "workspace"
            plan, applied = _apply(workspace)
            self.assertEqual(plan["counts"]["messages"], 6)
            self.assertEqual(plan["counts"]["topic_segments"], 2)
            self.assertEqual(plan["counts"]["raw_candidates"], 5)
            self.assertEqual(plan["counts"]["candidates"], 5)
            self.assertEqual(plan["counts"]["consolidated_candidates"], 4)
            self.assertEqual(plan["counts"]["open_questions"], 1)
            self.assertEqual(plan["counts"]["observation_generation_issues"], 0)
            self.assertGreaterEqual(plan["counts"]["observations"], 1)
            self.assertGreaterEqual(plan["counts"]["state_transitions"], 1)
            run_dir = Path(applied["run_dir"])
            segments = _records(run_dir / "segments.jsonl")
            candidates = _records(run_dir / "candidates.jsonl")
            open_questions = _records(run_dir / "open_questions.jsonl")

            self.assertEqual([item["topic_key"] for item in segments], ["scope", "apply"])
            self.assertIn("explicit_topic_boundary", segments[1]["boundary_reasons"])
            self.assertIn("topic_key_changed", segments[1]["boundary_reasons"])
            self.assertEqual(
                {item["candidate_type"] for item in candidates},
                {"decision", "solution", "constraint", "fact"},
            )
            self.assertTrue(all(item["evidence"][0]["verified"] for item in candidates))
            self.assertEqual(len(open_questions), 1)
            self.assertEqual(open_questions[0]["status"], "open")
            self.assertFalse(open_questions[0]["resolved"])
            self.assertEqual(open_questions[0]["project_id"], "project_synthetic")

            bundle = _records(run_dir / "review_bundle.jsonl")
            self.assertTrue(any(item["item_type"] == "open_question" for item in bundle))
            self.assertFalse(any(item.get("candidate_type") == "open_question" for item in candidates))

            stats = json.loads((run_dir / "review_stats.json").read_text(encoding="utf-8"))
            self.assertTrue(stats["suggestion_only"])
            self.assertEqual(stats["decisions_recorded"], 0)
            self.assertEqual(stats["open_questions_auto_closed"], 0)

    def test_evidence_validation_rejects_tampered_quote_and_hash(self) -> None:
        parsed = parse_controlled_jsonl(
            input_root=FIXTURE_ROOT,
            input_path=FIXTURE,
            source_id="src_synthetic",
            allowed_conversation_ids=(CONVERSATION_ID,),
        )
        scope = Scope(
            workspace_id="ws_synthetic",
            project_id="project_synthetic",
            source_id="src_synthetic",
            domain_profile="generic",
            profile_version="1.0.0",
            conversation_ids=(CONVERSATION_ID,),
            boundary_type="explicit_scan_boundary",
            boundary_evidence="unit evidence test",
        )
        segments = segment_messages(parsed.messages)
        extraction = extract_candidates(messages=parsed.messages, segments=segments, scope=scope)
        candidate = next(item for item in extraction.candidates if item.candidate_type == "decision")
        by_unit = {item.source_unit_id: item for item in parsed.messages}

        original = validate_evidence_span(candidate.evidence[0], messages_by_source_unit=by_unit)
        self.assertTrue(original.valid)

        tampered_quote = replace(candidate.evidence[0], quote="tampered quote")
        quote_result = validate_evidence_span(tampered_quote, messages_by_source_unit=by_unit)
        self.assertFalse(quote_result.valid)
        self.assertIn("quote_matches_source", quote_result.errors)

        tampered_hash = replace(candidate.evidence[0], source_unit_hash="0" * 64)
        hash_result = validate_evidence_span(tampered_hash, messages_by_source_unit=by_unit)
        self.assertFalse(hash_result.valid)
        self.assertIn("source_unit_hash_matches", hash_result.errors)

    def test_secret_scan_blocks_without_echo_or_write(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            input_root = root / "input"
            input_root.mkdir()
            input_path = input_root / "secret.jsonl"
            secret_value = "sk-test-SUPER-SECRET-VALUE"
            row = _message(0, text=f"FACT: API_KEY={secret_value}")
            input_path.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
            workspace = root / "workspace"
            plan = _plan(workspace, input_root=input_root, input_path=input_path)
            self.assertEqual(plan["status"], "blocked")
            self.assertFalse(workspace.exists())
            self.assertNotIn(secret_value, json.dumps(plan, ensure_ascii=False))
            codes = {item["code"] for item in plan["errors"]}
            self.assertIn("secret_scan_blocked", codes)

    def test_standalone_openai_keys_are_case_insensitive_and_not_echoed(self) -> None:
        values = (
            "sk-proj-AbC123_xYz-456789",
            "sk-lowercase_key-1234567890",
        )
        for value in values:
            with self.subTest(value_kind="standalone_openai_key"):
                with tempfile.TemporaryDirectory() as temp:
                    root = Path(temp)
                    input_root = root / "input"
                    input_root.mkdir()
                    input_path = input_root / "standalone_key.jsonl"
                    input_path.write_text(
                        json.dumps(_message(0, text=f"DECISION: keep {value}"), ensure_ascii=False) + "\n",
                        encoding="utf-8",
                    )
                    plan = _plan(root / "workspace", input_root=input_root, input_path=input_path)
                    self.assertEqual(plan["status"], "blocked")
                    encoded = json.dumps(plan, ensure_ascii=False)
                    self.assertNotIn(value, encoded)
                    self.assertIn(
                        "secret_scan_blocked",
                        {item["code"] for item in plan["errors"]},
                    )

    def test_existing_secret_categories_remain_covered(self) -> None:
        text = "\n".join([
            "Authorization: Bearer abcdefghijklmnop",
            "api_key=abcdefgh1234",
            "token: abcdefgh1234",
            "-----BEGIN OPENSSH PRIVATE KEY-----",
            "postgres://user:password@example.com/db",
            "sk-learn is not an API key",
        ])
        categories = {
            category
            for finding in scan_text_for_secrets(text)
            for category in finding["categories"]
        }
        self.assertTrue({
            "authorization_bearer",
            "api_key_assignment",
            "private_key",
            "credentialed_uri",
        } <= categories)
        self.assertNotIn("synthetic_openai_key", categories)

    def test_explicit_and_heuristic_candidates_use_quote_verified(self) -> None:
        rows = [
            _message(0, text="DECISION: 保留受控 API。"),
            _message(1, text="REQUIREMENT: 必须保留审计记录。"),
            _message(2, text="PREFERENCE: 优先使用本地验证。"),
            _message(3, text="必须保留完整 evidence。"),
        ]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            input_root = root / "input"
            input_root.mkdir()
            input_path = input_root / "verification_state.jsonl"
            input_path.write_text(
                "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
                encoding="utf-8",
            )
            plan = _plan(root / "workspace", input_root=input_root, input_path=input_path)
            self.assertEqual(plan["status"], "planned")
            applied = clean_apply(plan, confirmation_token=plan["confirmation_token"])
            self.assertEqual(applied["status"], "applied")
            candidates = _records(Path(applied["run_dir"]) / "raw_candidates.jsonl")
            self.assertTrue(candidates)
            self.assertTrue(all(item["verification_state"] == "quote_verified" for item in candidates))
            self.assertTrue({
                "user_decision",
                "user_request",
                "user_preference",
            } <= {item["assertion_source"] for item in candidates})

    def test_secret_path_is_rejected_before_read(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            secret_root = root / ".secrets"
            secret_root.mkdir()
            input_path = secret_root / "input.jsonl"
            input_path.write_text(json.dumps(_message(0), ensure_ascii=False) + "\n", encoding="utf-8")
            plan = _plan(root / "workspace", input_root=secret_root, input_path=input_path)
            self.assertEqual(plan["status"], "blocked")
            self.assertIn("unsafe_path", {item["code"] for item in plan["errors"]})
            self.assertFalse((root / "workspace").exists())

    def test_wrong_confirmation_and_stale_plan_do_not_write(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            input_root = root / "input"
            input_root.mkdir()
            input_path = input_root / "conversation.jsonl"
            original = [_message(0, text="DECISION: keep the first version")]
            input_path.write_text(
                "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in original),
                encoding="utf-8",
            )
            workspace = root / "workspace"
            plan = _plan(workspace, input_root=input_root, input_path=input_path)
            wrong = clean_apply(plan, confirmation_token="0" * 24)
            self.assertEqual(wrong["status"], "blocked")
            self.assertFalse(workspace.exists())

            original.append(_message(1, text="FACT: source changed after planning"))
            input_path.write_text(
                "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in original),
                encoding="utf-8",
            )
            stale = clean_apply(plan, confirmation_token=plan["confirmation_token"])
            self.assertEqual(stale["status"], "blocked")
            self.assertIn(stale["error"]["code"], {"plan_blocked", "conversation_cleaner_error"})
            self.assertFalse(workspace.exists())

    def test_no_production_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp) / "workspace"
            production = workspace / "production"
            production.mkdir(parents=True)
            canonical = production / "canonical.sqlite3"
            collection = production / "milvus" / "collection.json"
            alias = production / "alias.json"
            collection.parent.mkdir()
            canonical.write_bytes(b"production-canonical-placeholder")
            collection.write_text('{"collection":"prod_placeholder"}\n', encoding="utf-8")
            alias.write_text('{"alias":"prod_placeholder"}\n', encoding="utf-8")
            before = _hash_tree(production)

            _, applied = _apply(workspace)
            self.assertEqual(applied["status"], "applied")
            self.assertEqual(before, _hash_tree(production))
            self.assertTrue((workspace / "conversation-cleaner" / "v1").is_dir())
            for name in ("canonical.sqlite3", "alias.json", "collection.json"):
                matches = list(workspace.rglob(name))
                self.assertEqual(len(matches), 1, f"unexpected production-like artifact: {name}")

    def test_review_export_is_explicit_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp) / "workspace"
            _, applied = _apply(workspace)
            export_one = review_export(workspace_root=workspace, run_id=applied["run_id"])
            self.assertEqual(export_one["status"], "exported")
            output = Path(export_one["output_dir"])
            self.assertTrue((output / "review_bundle.jsonl").is_file())
            self.assertTrue((output / "audit_report.json").is_file())
            self.assertTrue((output / "observations.jsonl").is_file())
            self.assertTrue((output / "state_transitions.jsonl").is_file())
            self.assertTrue((output / "state_review.jsonl").is_file())
            export_manifest = json.loads((output / "export_manifest.json").read_text(encoding="utf-8"))
            self.assertTrue(
                {"observations.jsonl", "state_transitions.jsonl", "state_review.jsonl"}
                <= set(export_manifest["artifacts"])
            )
            export_two = review_export(workspace_root=workspace, run_id=applied["run_id"])
            self.assertEqual(export_two["status"], "already_exported")
            self.assertFalse(export_two["changed"])

    def test_doctor_is_read_only_and_reports_no_external_dependencies(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp) / "workspace"
            result = doctor(workspace_root=workspace, input_root=FIXTURE_ROOT, input_path=FIXTURE)
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["external_dependencies"], [])
            self.assertTrue(result["checks"]["stdlib_only"])
            self.assertFalse(workspace.exists())


if __name__ == "__main__":
    unittest.main()


class CleanerPresetTests(unittest.TestCase):
    def test_clean_plan_accepts_preset_defaults(self) -> None:
        import contextlib
        import io

        from conversation_cleaner.cli import main as cli_main

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "workspace"
            preset = root / "cleaner-preset.json"
            preset.write_text(json.dumps({
                "contract_version": "conversation-cleaner-preset-v1",
                "input_root": str(FIXTURE_ROOT),
                "input": str(FIXTURE),
                "workspace_root": str(workspace),
                "workspace_id": "ws_synthetic",
                "project_id": "project_synthetic",
                "source_id": "src_synthetic",
                "domain_profile": "generic",
                "profile_version": "1.0.0",
                "conversation_id": [CONVERSATION_ID],
                "boundary_type": "explicit_scan_boundary",
                "boundary_evidence": "explicit synthetic acceptance run",
            }), encoding="utf-8")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = cli_main(["clean", "plan", "--preset", str(preset), "--json"])
            payload = json.loads(output.getvalue())
            self.assertEqual(code, 0)
            self.assertEqual(payload["status"], "planned")
            self.assertEqual(payload["scope"]["workspace_id"], "ws_synthetic")
            self.assertFalse(workspace.exists())
