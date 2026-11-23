from __future__ import annotations

import contextlib
import hashlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from conversation_cleaner.api_v1 import (  # noqa: E402
    ConversationSkillApi,
    handoff_review_status,
)
from conversation_cleaner.cli import main  # noqa: E402
from conversation_cleaner.contracts import CONVERSATION_SKILL_API_VERSION  # noqa: E402
from conversation_cleaner.handoff_contracts import MemoryWriteHandoffItem  # noqa: E402
from conversation_cleaner.storage import state_root  # noqa: E402


HANDOFF_ID = "handoff_111111111111111111111111"
ITEM_KEY = "mwh_cli_review_1"
REVIEWED_AT = "2026-11-22T12:00:00+08:00"


def _item(key: str = ITEM_KEY) -> MemoryWriteHandoffItem:
    content = "CLI approval review memory."
    return MemoryWriteHandoffItem.from_dict(
        {
            "idempotency_key": key,
            "operation_id": "op_" + key,
            "workspace_id": "ws_cli_review",
            "project_id": "project_cli_review",
            "task_id": None,
            "conversation_id": "conv_cli_review",
            "domain_profile": "generic",
            "profile_version": "1.0.0",
            "memory_type": "fact",
            "subtype": "fact",
            "subject": "CLI review",
            "entity_type": "topic",
            "entity_id": "topic-cli",
            "content": content,
            "importance": "medium",
            "declared_sensitivity": "private",
            "requested_visibility": "private",
            "assertion_source": "user_report",
            "verification_state": "quote_verified",
            "effective_from": None,
            "effective_to": None,
            "source_id": "src_cli_review",
            "source_unit_id": "su_cli_review",
            "source_scope": {
                "scope_type": "conversation_message",
                "ref": "msg_cli_review",
                "start": 0,
                "end": len(content),
                "locator_json": {"message_id": "msg_cli_review"},
            },
            "confidence": 0.9,
            "supersedes_ref": None,
            "supersedes_id": None,
            "expected_version": None,
            "domain_payload": {"source_sequence": 0},
            "evidences": [
                {
                    "evidence_id": "ev_" + key,
                    "source_unit_id": "su_cli_review",
                    "quote": content,
                    "locator_json": {"message_id": "msg_cli_review", "char_start": 0, "char_end": len(content)},
                    "status": "verified",
                }
            ],
            "claim_key": "claim:cli-review",
            "scope_key": "workspace:ws_cli_review|project:project_cli_review|subject:cli-review",
            "observed_at": "2026-11-22T10:00:00+08:00",
            "transition_kind": "new",
        }
    )


def _make_handoff(root: Path) -> tuple[Path, Path, MemoryWriteHandoffItem]:
    workspace = root / "workspace"
    handoff_dir = state_root(workspace) / "handoffs" / HANDOFF_ID
    handoff_dir.mkdir(parents=True)
    item = _item()
    (handoff_dir / "memory_write_batch.jsonl").write_text(
        json.dumps(item.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    (handoff_dir / "handoff_manifest.json").write_text(
        json.dumps({"handoff_id": HANDOFF_ID}, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return workspace, handoff_dir, item


def _run_cli(args: list[str]) -> tuple[int, dict]:
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = main(args)
    return code, json.loads(buffer.getvalue())


def _decide_args(workspace: Path, decision: str, reviewed_at: str = REVIEWED_AT, key: str = ITEM_KEY) -> list[str]:
    return [
        "handoff", "review", "decide",
        "--workspace-root", str(workspace),
        "--handoff-id", HANDOFF_ID,
        "--idempotency-key", key,
        "--decision", decision,
        "--reviewer-id", "reviewer-1",
        "--reason", f"CLI {decision} decision.",
        "--reviewed-at", reviewed_at,
        "--json",
    ]


def _hash_tree(directory: Path) -> dict[str, str]:
    return {
        path.relative_to(directory).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }



def test_auto_approve_only_safe_user_decisions() -> None:
    with tempfile.TemporaryDirectory() as temp:
        workspace = Path(temp) / "workspace"
        handoff_dir = state_root(workspace) / "handoffs" / HANDOFF_ID
        handoff_dir.mkdir(parents=True)
        safe = _item("mwh_auto_safe").to_dict()
        safe["assertion_source"] = "user_decision"
        safe["verification_state"] = "quote_verified"
        safe["domain_payload"] = {"source_sequence": 0}
        risky = _item("mwh_auto_risky").to_dict()
        risky["assertion_source"] = "assistant_inference"
        risky["domain_payload"] = {"split_review": True}
        (handoff_dir / "memory_write_batch.jsonl").write_text(
            json.dumps(safe, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
            + json.dumps(risky, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        (handoff_dir / "handoff_manifest.json").write_text(
            json.dumps({"handoff_id": HANDOFF_ID}, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        code, payload = _run_cli([
            "handoff", "review", "auto-approve",
            "--workspace-root", str(workspace),
            "--handoff-id", HANDOFF_ID,
            "--reviewed-at", REVIEWED_AT,
            "--json",
        ])
        assert code == 0
        assert payload["status"] == "recorded"
        assert payload["recorded"] == ["mwh_auto_safe"]
        assert payload["counts"]["recorded"] == 1
        assert payload["counts"]["skipped"] == 1
        assert payload["skipped"][0]["idempotency_key"] == "mwh_auto_risky"
        status = handoff_review_status(workspace_root=workspace, handoff_id=HANDOFF_ID)
        assert status["counts"]["approved"] == 1
        assert status["counts"]["pending"] == 1
        assert "handoff_review_auto_approve" in ConversationSkillApi


class HandoffReviewCliTests(unittest.TestCase):
    def test_cli_approve_status_export_duplicate_and_revoke(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            workspace, handoff_dir, _ = _make_handoff(Path(temp))
            export_args = ["handoff", "review", "export-approved", "--workspace-root", str(workspace), "--handoff-id", HANDOFF_ID, "--json"]
            code, pending_export = _run_cli(export_args)
            self.assertEqual(code, 0)
            self.assertEqual(pending_export["counts"]["pending"], 1)
            self.assertEqual((handoff_dir / "approved_memory_write_batch.jsonl").read_text(encoding="utf-8"), "")

            code, decided = _run_cli(_decide_args(workspace, "approve"))
            self.assertEqual(code, 0)
            self.assertEqual(decided["status"], "recorded")
            self.assertTrue(decided["changed"])

            code, duplicate = _run_cli(_decide_args(workspace, "approve"))
            self.assertEqual(code, 0)
            self.assertEqual(duplicate["status"], "duplicate")
            self.assertFalse(duplicate["changed"])

            before_status = _hash_tree(handoff_dir)
            code, status_payload = _run_cli(
                ["handoff", "review", "status", "--workspace-root", str(workspace), "--handoff-id", HANDOFF_ID, "--json"]
            )
            self.assertEqual(code, 0)
            self.assertEqual(status_payload["status"], "ok")
            self.assertEqual(status_payload["counts"]["approved"], 1)
            self.assertEqual(status_payload["items"][0]["latest_decision"], "approve")
            self.assertEqual(len(status_payload["ledger_sha256"]), 64)
            self.assertFalse(status_payload["changed"])
            self.assertEqual(before_status, _hash_tree(handoff_dir))

            code, exported = _run_cli(export_args)
            self.assertEqual(code, 0)
            self.assertEqual(exported["status"], "generated")
            self.assertEqual(exported["counts"]["approved"], 1)
            self.assertEqual(len((handoff_dir / "approved_memory_write_batch.jsonl").read_text(encoding="utf-8").splitlines()), 1)
            code, exported_again = _run_cli(export_args)
            self.assertEqual(code, 0)
            self.assertEqual(exported_again["status"], "already_generated")
            self.assertFalse(exported_again["changed"])

            code, revoked = _run_cli(_decide_args(workspace, "revoke", "2026-11-22T12:10:00+08:00"))
            self.assertEqual(code, 0)
            self.assertEqual(revoked["status"], "recorded")
            code, revoked_export = _run_cli(export_args)
            self.assertEqual(code, 0)
            self.assertEqual(revoked_export["counts"], {"approved": 0, "rejected": 0, "deferred": 0, "revoked": 1, "pending": 0})
            self.assertEqual((handoff_dir / "approved_memory_write_batch.jsonl").read_text(encoding="utf-8"), "")

    def test_cli_reject_and_defer_are_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            workspace, handoff_dir, _ = _make_handoff(Path(temp))
            export_args = ["handoff", "review", "export-approved", "--workspace-root", str(workspace), "--handoff-id", HANDOFF_ID, "--json"]
            code, _ = _run_cli(_decide_args(workspace, "reject", "2026-11-22T12:00:00+08:00"))
            self.assertEqual(code, 0)
            code, rejected = _run_cli(export_args)
            self.assertEqual(code, 0)
            self.assertEqual(rejected["counts"]["rejected"], 1)
            self.assertEqual((handoff_dir / "approved_memory_write_batch.jsonl").read_text(encoding="utf-8"), "")

            code, _ = _run_cli(_decide_args(workspace, "defer", "2026-11-22T12:05:00+08:00"))
            self.assertEqual(code, 0)
            code, deferred = _run_cli(export_args)
            self.assertEqual(code, 0)
            self.assertEqual(deferred["counts"]["deferred"], 1)
            self.assertEqual((handoff_dir / "approved_memory_write_batch.jsonl").read_text(encoding="utf-8"), "")

    def test_api_surface_unknowns_paths_symlinks_and_invalid_values(self) -> None:
        self.assertIn("handoff_review_decide", ConversationSkillApi)
        self.assertIn("handoff_review_status", ConversationSkillApi)
        self.assertIn("handoff_review_export_approved", ConversationSkillApi)
        self.assertEqual(CONVERSATION_SKILL_API_VERSION, "1.5.0")

        with tempfile.TemporaryDirectory() as temp:
            workspace, handoff_dir, _ = _make_handoff(Path(temp))
            code, unknown_item = _run_cli(_decide_args(workspace, "approve", key="mwh_missing"))
            self.assertEqual(code, 2)
            self.assertEqual(unknown_item["status"], "blocked")
            self.assertIn("unknown", unknown_item["error"]["message"])

            code, unknown_handoff = _run_cli([
                "handoff", "review", "status",
                "--workspace-root", str(workspace),
                "--handoff-id", "handoff_222222222222222222222222",
                "--json",
            ])
            self.assertEqual(code, 2)
            self.assertEqual(unknown_handoff["status"], "blocked")

            code, path_escape = _run_cli([
                "handoff", "review", "status",
                "--workspace-root", str(workspace),
                "--handoff-id", "../escape",
                "--json",
            ])
            self.assertEqual(code, 2)
            self.assertEqual(path_escape["status"], "blocked")

            with self.assertRaises(SystemExit) as invalid_decision:
                with contextlib.redirect_stderr(io.StringIO()):
                    main(_decide_args(workspace, "accepted"))
            self.assertNotEqual(invalid_decision.exception.code, 0)

            code, invalid_time = _run_cli(_decide_args(workspace, "approve", reviewed_at="2026-11-22T12:00:00"))
            self.assertEqual(code, 2)
            self.assertEqual(invalid_time["status"], "blocked")

            symlink_path = state_root(workspace) / "handoffs" / "handoff_333333333333333333333333"
            original_is_symlink = Path.is_symlink

            def simulated_is_symlink(path: Path) -> bool:
                if path == symlink_path:
                    return True
                return original_is_symlink(path)

            with mock.patch.object(Path, "is_symlink", simulated_is_symlink):
                symlink_result = handoff_review_status(workspace_root=workspace, handoff_id=symlink_path.name)
            self.assertEqual(symlink_result["status"], "blocked")
            self.assertIn("symlink", symlink_result["error"]["message"])

            ledger_unchanged = not (handoff_dir / "approval_ledger.jsonl").exists()
            self.assertTrue(ledger_unchanged)


if __name__ == "__main__":
    unittest.main()
