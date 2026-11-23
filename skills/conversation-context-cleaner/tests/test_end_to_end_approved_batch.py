from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from conversation_cleaner.api_v1 import clean_apply, clean_plan, handoff_export  # noqa: E402
from conversation_cleaner.cli import main  # noqa: E402
from conversation_cleaner.database_handoff_adapter import (  # noqa: E402
    DatabaseHandoffAdapterError,
    to_memory_write_input,
)
from conversation_cleaner.handoff_contracts import MemoryWriteHandoffItem  # noqa: E402
from conversation_cleaner.handoff_review import read_approval_ledger  # noqa: E402
from conversation_cleaner.state_resolution import build_scope_key  # noqa: E402


try:
    from memory_core.db import SQLiteStore
    from memory_core.models import (
        AccessGrant,
        AccessScope,
        EvidenceStatus,
        MemoryStatus,
        MemoryWriteInput,
        SensitivityLevel,
    )
    from memory_core.profiles import ProfileRegistry
    from memory_core.security import TokenValidator, issue_token
    from memory_core.writer import MemoryWriter

    MEMORY_CORE_AVAILABLE = True
except Exception:
    MEMORY_CORE_AVAILABLE = False


CONVERSATION_ID = "conv_approved_batch_e2e"
WORKSPACE_ID = "ws_approved_batch_e2e"
PROJECT_ID = "project_approved_batch_e2e"
SOURCE_ID = "src_approved_batch_e2e"
PRINCIPAL_ID = "approved-batch-reviewer"
SECRET = b"temporary-approved-batch-secret"
CLAIM_KEY = "claim:approved-batch-memory"
SCOPE_KEY = build_scope_key(workspace_id=WORKSPACE_ID, project_id=PROJECT_ID, subject="approved batch")
TEXT = "FACT: 已经有 32GB 内存。"


def _records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _row() -> dict:
    return {
        "conversation_id": CONVERSATION_ID,
        "message_id": "msg_approved_batch_000",
        "role": "user",
        "timestamp": "2026-11-22T10:00:00+08:00",
        "sequence": 0,
        "text": TEXT,
        "attachments": [],
        "metadata": {
            "topic_key": "approved-batch",
            "observation": {
                "claim_key": CLAIM_KEY,
                "scope_key": SCOPE_KEY,
                "state_kind": "fact",
            },
        },
    }


def _run_cli(args: list[str]) -> tuple[int, dict]:
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = main(args)
    return code, json.loads(buffer.getvalue())


def _apply_run(root: Path, workspace: Path) -> tuple[Path, dict, dict, dict]:
    input_root = root / "input"
    input_root.mkdir()
    input_path = input_root / "conversation.jsonl"
    input_path.write_text(json.dumps(_row(), ensure_ascii=False) + "\n", encoding="utf-8")
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
        boundary_evidence="isolated approved-batch end-to-end test",
    )
    self_status = plan["status"]
    if self_status != "planned":
        raise AssertionError(plan)
    applied = clean_apply(plan, confirmation_token=plan["confirmation_token"])
    if applied["status"] != "applied":
        raise AssertionError(applied)
    run_dir = Path(applied["run_dir"])
    exported = handoff_export(
        workspace_root=workspace,
        run_id=applied["run_id"],
        observations_root=run_dir,
        observations_path=run_dir / "observations.jsonl",
        workspace_id=WORKSPACE_ID,
        project_id=PROJECT_ID,
        domain_profile="generic",
        profile_version="1.0.0",
        source_id=SOURCE_ID,
    )
    if exported["status"] != "exported":
        raise AssertionError(exported)
    handoff_dir = Path(exported["output_dir"])
    batch = _records(handoff_dir / "memory_write_batch.jsonl")
    if len(batch) != 1:
        raise AssertionError(batch)
    return handoff_dir, exported, batch[0], _records(run_dir / "messages.jsonl")[0]


@unittest.skipUnless(
    MEMORY_CORE_AVAILABLE,
    "requires chat_etl_v1 venv and PYTHONPATH=E:\\污染治理---数据库\\memory_core_v1\\src",
)
class EndToEndApprovedBatchTests(unittest.TestCase):
    def test_cli_approval_ledger_batch_adapter_and_sqlite(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "workspace"
            handoff_dir, exported, raw_item, message = _apply_run(root, workspace)
            handoff_id = exported["handoff_id"]
            item = MemoryWriteHandoffItem.from_dict(raw_item)

            with self.assertRaisesRegex(DatabaseHandoffAdapterError, "approval rejected"):
                to_memory_write_input(item, handoff_id=handoff_id, approval=None)

            decide_args = [
                "handoff", "review", "decide",
                "--workspace-root", str(workspace),
                "--handoff-id", handoff_id,
                "--idempotency-key", item.idempotency_key,
                "--decision", "approve",
                "--reviewer-id", PRINCIPAL_ID,
                "--reason", "Approved after isolated evidence inspection.",
                "--reviewed-at", "2026-11-22T12:00:00+08:00",
                "--json",
            ]
            code, decided = _run_cli(decide_args)
            self.assertEqual(code, 0)
            self.assertEqual(decided["status"], "recorded")
            code, status = _run_cli([
                "handoff", "review", "status",
                "--workspace-root", str(workspace),
                "--handoff-id", handoff_id,
                "--json",
            ])
            self.assertEqual(code, 0)
            self.assertEqual(status["counts"]["approved"], 1)
            self.assertFalse(status["approved_batch_exists"])

            code, exported_approved = _run_cli([
                "handoff", "review", "export-approved",
                "--workspace-root", str(workspace),
                "--handoff-id", handoff_id,
                "--json",
            ])
            self.assertEqual(code, 0)
            self.assertEqual(exported_approved["counts"]["approved"], 1)
            approved_items = _records(handoff_dir / "approved_memory_write_batch.jsonl")
            self.assertEqual(len(approved_items), 1)
            approved_item = MemoryWriteHandoffItem.from_dict(approved_items[0])
            ledger = read_approval_ledger(handoff_dir)
            approval = max(ledger, key=lambda record: (datetime.fromisoformat(record.reviewed_at), record.approval_id))
            self.assertEqual(approval.decision, "approve")

            store = SQLiteStore(root / "approved_batch.sqlite3")
            store.migrate()
            store.ensure_workspace_project(
                workspace_id=WORKSPACE_ID,
                workspace_name="Approved batch workspace",
                project_id=PROJECT_ID,
                project_name="Approved batch project",
            )
            profiles = ProfileRegistry()
            store.register_profiles(profiles)
            source_sha256 = json.loads((handoff_dir / "handoff_manifest.json").read_text(encoding="utf-8"))["source_sha256"]
            store.insert_source(
                source_id=SOURCE_ID,
                workspace_id=WORKSPACE_ID,
                project_id=PROJECT_ID,
                visibility="workspace",
                owner_principal_id=None,
                source_path="memory://approved-batch-e2e",
                source_sha256=source_sha256,
                source_type="conversation",
                sensitivity=SensitivityLevel.PRIVATE.value,
            )
            store.insert_source_unit(
                source_unit_id=message["source_unit_id"],
                source_id=SOURCE_ID,
                parent_unit_id=None,
                scope_type="conversation_message",
                scope_ref=message["message_id"],
                source_unit_hash=message["source_unit_hash"],
                text_content=message["text"],
                metadata={"sequence": message["sequence"]},
            )
            now = datetime.now(timezone.utc)
            grant = AccessGrant(
                grant_id="grant-approved-batch-e2e",
                principal_id=PRINCIPAL_ID,
                workspace_id=WORKSPACE_ID,
                scope=AccessScope(
                    project_ids=[PROJECT_ID],
                    domains=["generic"],
                    max_sensitivity=SensitivityLevel.PRIVATE,
                    allow_global_kb=False,
                ),
                actions=["write", "search"],
                expires_at=now + timedelta(hours=1),
                remaining_queries=None,
                purpose="isolated approved-batch e2e",
                created_at=now,
            )
            store.save_grant(grant)
            token_validator = TokenValidator(store, secret=SECRET)
            writer = MemoryWriter(store=store, profiles=profiles, token_validator=token_validator)

            def fresh_token():
                return issue_token(
                    secret=SECRET,
                    principal_id=PRINCIPAL_ID,
                    task_id="approved-batch-e2e",
                    grant_id=grant.grant_id,
                    workspace_id=WORKSPACE_ID,
                    ttl_seconds=300,
                )

            payload = to_memory_write_input(
                approved_item,
                handoff_id=handoff_id,
                approval=approval,
            )
            self.assertEqual({evidence["status"] for evidence in payload["evidences"]}, {"verified"})
            write_input = MemoryWriteInput.model_validate(payload)
            self.assertTrue(all(evidence.status == EvidenceStatus.VERIFIED for evidence in write_input.evidences))
            result = writer.write(write_input, fresh_token())
            self.assertEqual(result.version, 1)
            self.assertEqual(result.status, MemoryStatus.ACTIVE)
            record = store.get_memory_record(result.memory_id)
            self.assertEqual(record.status, MemoryStatus.ACTIVE)
            self.assertEqual({evidence.quote for evidence in record.evidences}, {TEXT.removeprefix("FACT: ")})

            revoke_args = list(decide_args)
            revoke_args[revoke_args.index("--decision") + 1] = "revoke"
            revoke_args[revoke_args.index("--reviewed-at") + 1] = "2026-11-22T12:05:00+08:00"
            revoke_args[revoke_args.index("--reason") + 1] = "Revoked after batch inspection."
            code, revoked = _run_cli(revoke_args)
            self.assertEqual(code, 0)
            self.assertEqual(revoked["status"], "recorded")
            code, revoked_batch = _run_cli([
                "handoff", "review", "export-approved",
                "--workspace-root", str(workspace),
                "--handoff-id", handoff_id,
                "--json",
            ])
            self.assertEqual(code, 0)
            self.assertEqual(revoked_batch["counts"]["approved"], 0)
            self.assertEqual(revoked_batch["counts"]["revoked"], 1)
            self.assertEqual((handoff_dir / "approved_memory_write_batch.jsonl").read_text(encoding="utf-8"), "")

            revoke_record = max(
                read_approval_ledger(handoff_dir),
                key=lambda record: (datetime.fromisoformat(record.reviewed_at), record.approval_id),
            )
            with self.assertRaisesRegex(DatabaseHandoffAdapterError, "only an approve decision"):
                to_memory_write_input(item, handoff_id=handoff_id, approval=revoke_record)
            with store.read() as connection:
                count = connection.execute(
                    "SELECT COUNT(*) AS count FROM memory_items WHERE workspace_id=? AND project_id=?",
                    (WORKSPACE_ID, PROJECT_ID),
                ).fetchone()["count"]
            self.assertEqual(count, 1)
            approval_manifest = json.loads((handoff_dir / "approval_manifest.json").read_text(encoding="utf-8"))
            for field in ("production_writes", "real_llm_called", "milvus_written", "alias_changed"):
                self.assertIs(approval_manifest[field], False)


if __name__ == "__main__":
    unittest.main()
