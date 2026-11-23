from __future__ import annotations

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

from conversation_cleaner.api_v1 import (  # noqa: E402
    ConversationSkillApi,
    clean_apply,
    clean_plan,
    handoff_export,
)
from conversation_cleaner.database_handoff_adapter import (  # noqa: E402
    DatabaseHandoffAdapterError,
    to_memory_write_input,
)
from conversation_cleaner.handoff_approval import create_handoff_approval  # noqa: E402
from conversation_cleaner.handoff_contracts import MemoryWriteHandoffItem  # noqa: E402
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
    MEMORY_CORE_IMPORT_ERROR = ""
except Exception as exc:  # pragma: no cover - exercised only by the standard-library runner
    MEMORY_CORE_AVAILABLE = False
    MEMORY_CORE_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"


CONVERSATION_ID = "conv_e2e_incremental"
WORKSPACE_ID = "ws_e2e_incremental"
PROJECT_ID = "project_e2e_incremental"
SOURCE_ID = "src_e2e_incremental"
PRINCIPAL_ID = "e2e-reviewer"
SECRET = b"temporary-e2e-handoff-secret"
CLAIM_KEY = "claim:device-memory-upgrade"
SCOPE_KEY = build_scope_key(
    workspace_id=WORKSPACE_ID,
    project_id=PROJECT_ID,
    subject="device memory",
)
OLD_TEXT = "暂时不升级内存，因为没有预算。"
NEW_TEXT = "已经升级到 32GB 内存。"


def _records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]



def _row(sequence: int, text: str, state_kind: str) -> dict:
    return {
        "conversation_id": CONVERSATION_ID,
        "message_id": f"msg_e2e_{sequence:03d}",
        "role": "user",
        "timestamp": f"2026-11-22T10:{sequence:02d}:00+08:00",
        "sequence": sequence,
        "text": text,
        "attachments": [],
        "metadata": {
            "topic_key": "device-memory",
            "observation": {
                "claim_key": CLAIM_KEY,
                "scope_key": SCOPE_KEY,
                "state_kind": state_kind,
            },
        },
    }


def _make_run(root: Path, workspace_root: Path, *, slug: str, rows: list[dict]) -> dict:
    input_root = root / f"input_{slug}"
    input_root.mkdir()
    input_path = input_root / "conversation.jsonl"
    input_path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    plan = clean_plan(
        input_root=input_root,
        input_path=input_path,
        workspace_root=workspace_root,
        workspace_id=WORKSPACE_ID,
        project_id=PROJECT_ID,
        source_id=SOURCE_ID,
        domain_profile="generic",
        profile_version="1.0.0",
        conversation_ids=[CONVERSATION_ID],
        boundary_type="conversation_closed",
        boundary_evidence=f"isolated end-to-end run {slug}",
    )
    if plan["status"] != "planned":
        raise AssertionError(plan)
    applied = clean_apply(plan, confirmation_token=plan["confirmation_token"])
    if applied["status"] != "applied":
        raise AssertionError(applied)
    run_dir = Path(applied["run_dir"])
    observations = _records(run_dir / "observations.jsonl")
    transitions = _records(run_dir / "state_transitions.jsonl")
    messages = _records(run_dir / "messages.jsonl")
    exported = handoff_export(
        workspace_root=workspace_root,
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
    manifest = json.loads((handoff_dir / "handoff_manifest.json").read_text(encoding="utf-8"))
    return {
        "plan": plan,
        "applied": applied,
        "run_dir": run_dir,
        "observations": observations,
        "transitions": transitions,
        "messages": messages,
        "export": exported,
        "handoff_dir": handoff_dir,
        "batch": batch,
        "manifest": manifest,
    }


@unittest.skipUnless(
    MEMORY_CORE_AVAILABLE,
    "requires chat_etl_v1 venv and PYTHONPATH=E:\\污染治理---数据库\\memory_core_v1\\src",
)
class EndToEndHandoffTests(unittest.TestCase):
    def test_clean_runs_to_isolated_sqlite_supersede_and_idempotency(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace_root = root / "workspace"
            run_one = _make_run(
                root,
                workspace_root,
                slug="run1",
                rows=[_row(0, f"DECISION: {OLD_TEXT}", "constraint")],
            )
            run_two = _make_run(
                root,
                workspace_root,
                slug="run2",
                rows=[
                    _row(0, f"DECISION: {OLD_TEXT}", "constraint"),
                    _row(1, f"FACT: {NEW_TEXT}", "fact"),
                ],
            )

            self.assertIn("clean_plan", ConversationSkillApi)
            self.assertIn("clean_apply", ConversationSkillApi)
            self.assertIn("handoff_export", ConversationSkillApi)
            self.assertEqual(run_one["plan"]["counts"]["observations"], 1)
            self.assertEqual(run_one["plan"]["counts"]["state_transitions"], 1)
            self.assertEqual(run_two["plan"]["counts"]["observations"], 2)
            self.assertEqual(run_two["plan"]["counts"]["state_transitions"], 2)
            for run in (run_one, run_two):
                self.assertEqual(
                    {(item["claim_key"], item["scope_key"]) for item in run["observations"]},
                    {(CLAIM_KEY, SCOPE_KEY)},
                )
            self.assertEqual(len(run_one["transitions"]), 1)
            supersedes = [item for item in run_two["transitions"] if item["transition_kind"] == "supersedes"]
            self.assertEqual(len(supersedes), 1)
            self.assertEqual(len(run_one["batch"]), 1)
            self.assertEqual(len(run_two["batch"]), 1)
            self.assertEqual(run_two["batch"][0]["transition_kind"], "supersedes")

            for run in (run_one, run_two):
                manifest = run["manifest"]
                self.assertIs(manifest["production_writes"], False)
                self.assertIs(manifest["real_llm_called"], False)
                self.assertIs(manifest["milvus_written"], False)
                self.assertIs(manifest["alias_changed"], False)
                self.assertIs(manifest["canonical_writes"], False)
                self.assertIs(manifest["contains_security_fields"], False)

            store = SQLiteStore(root / "memory.sqlite3")
            store.migrate()
            store.ensure_workspace_project(
                workspace_id=WORKSPACE_ID,
                workspace_name="End-to-end handoff workspace",
                project_id=PROJECT_ID,
                project_name="End-to-end handoff project",
            )
            profiles = ProfileRegistry()
            store.register_profiles(profiles)

            source_sha256 = run_two["manifest"]["source_sha256"]
            store.insert_source(
                source_id=SOURCE_ID,
                workspace_id=WORKSPACE_ID,
                project_id=PROJECT_ID,
                visibility="workspace",
                owner_principal_id=None,
                source_path="memory://isolated-e2e-handoff",
                source_sha256=source_sha256,
                source_type="conversation",
                sensitivity=SensitivityLevel.PRIVATE.value,
            )
            seen_units: set[str] = set()
            for message in run_one["messages"] + run_two["messages"]:
                source_unit_id = message["source_unit_id"]
                if source_unit_id in seen_units:
                    continue
                seen_units.add(source_unit_id)
                store.insert_source_unit(
                    source_unit_id=source_unit_id,
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
                grant_id="grant-e2e-handoff",
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
                purpose="isolated end-to-end handoff validation",
                created_at=now,
            )
            store.save_grant(grant)
            token_validator = TokenValidator(store, secret=SECRET)
            writer = MemoryWriter(store=store, profiles=profiles, token_validator=token_validator)

            def fresh_token():
                return issue_token(
                    secret=SECRET,
                    principal_id=PRINCIPAL_ID,
                    task_id="e2e-handoff-task",
                    grant_id=grant.grant_id,
                    workspace_id=WORKSPACE_ID,
                    ttl_seconds=300,
                )

            old_item = MemoryWriteHandoffItem.from_dict(run_one["batch"][0])
            old_handoff_id = run_one["export"]["handoff_id"]
            old_approval = create_handoff_approval(
                item=old_item,
                handoff_id=old_handoff_id,
                decision="approve",
                reviewer_id=PRINCIPAL_ID,
                reason="Approved isolated Run 1 handoff after evidence review.",
                reviewed_at="2026-11-22T12:00:00+08:00",
            )
            old_payload = to_memory_write_input(
                old_item,
                handoff_id=old_handoff_id,
                approval=old_approval,
            )
            self.assertEqual(
                old_payload["domain_payload"]["conversation_handoff"]["approval_id"],
                old_approval.approval_id,
            )
            old_input = MemoryWriteInput.model_validate(old_payload)
            old_result = writer.write(old_input, fresh_token())
            self.assertEqual(old_result.version, 1)
            self.assertEqual(old_result.status, MemoryStatus.ACTIVE)
            self.assertEqual(store.get_memory_record(old_result.memory_id).status, MemoryStatus.ACTIVE)

            new_item = MemoryWriteHandoffItem.from_dict(run_two["batch"][0])
            self.assertEqual(new_item.transition_kind, "supersedes")
            self.assertEqual(new_item.claim_key, CLAIM_KEY)
            self.assertEqual(new_item.scope_key, SCOPE_KEY)
            run_two_old = min(run_two["observations"], key=lambda item: item["source_sequence"])
            self.assertEqual(new_item.supersedes_ref, run_two_old["observation_id"])
            self.assertEqual(new_item.expected_version, None)

            new_handoff_id = run_two["export"]["handoff_id"]
            with self.assertRaisesRegex(DatabaseHandoffAdapterError, "approval rejected"):
                to_memory_write_input(
                    new_item,
                    handoff_id=new_handoff_id,
                    approval=None,
                    supersedes_ref_to_id={new_item.supersedes_ref: old_result.memory_id},
                    expected_version=1,
                )

            new_approval = create_handoff_approval(
                item=new_item,
                handoff_id=new_handoff_id,
                decision="approve",
                reviewer_id=PRINCIPAL_ID,
                reason="Approved isolated Run 2 superseding handoff after evidence review.",
                reviewed_at="2026-11-22T12:05:00+08:00",
            )
            new_payload = to_memory_write_input(
                new_item,
                handoff_id=new_handoff_id,
                approval=new_approval,
                supersedes_ref_to_id={new_item.supersedes_ref: old_result.memory_id},
                expected_version=1,
            )
            self.assertEqual({item["status"] for item in new_payload["evidences"]}, {"verified"})
            self.assertEqual(
                new_payload["domain_payload"]["conversation_handoff"]["approval_id"],
                new_approval.approval_id,
            )
            new_input = MemoryWriteInput.model_validate(new_payload)
            self.assertTrue(all(item.status == EvidenceStatus.VERIFIED for item in new_input.evidences))
            self.assertEqual(new_input.supersedes_id, old_result.memory_id)
            self.assertEqual(new_input.expected_version, 1)
            self.assertEqual(new_input.source_id, SOURCE_ID)
            self.assertEqual(new_payload["domain_payload"]["conversation_handoff"]["claim_key"], CLAIM_KEY)
            self.assertEqual(new_payload["domain_payload"]["conversation_handoff"]["scope_key"], SCOPE_KEY)
            self.assertNotIn("claim_key", new_payload)
            self.assertNotIn("scope_key", new_payload)

            new_result = writer.write(new_input, fresh_token())
            self.assertEqual(new_result.version, 2)
            self.assertEqual(new_result.status, MemoryStatus.ACTIVE)
            old_record = store.get_memory_record(old_result.memory_id)
            new_record = store.get_memory_record(new_result.memory_id)
            self.assertEqual(old_record.status, MemoryStatus.SUPERSEDED)
            self.assertEqual(new_record.status, MemoryStatus.ACTIVE)
            self.assertEqual(new_record.version, 2)
            self.assertEqual(new_record.supersedes_id, old_result.memory_id)
            self.assertEqual(new_record.domain_payload["conversation_handoff"]["claim_key"], CLAIM_KEY)
            self.assertEqual(new_record.domain_payload["conversation_handoff"]["scope_key"], SCOPE_KEY)
            self.assertEqual({item.quote for item in new_record.evidences}, {OLD_TEXT, NEW_TEXT})

            repeated = writer.write(new_input, fresh_token())
            self.assertEqual(repeated.memory_id, new_result.memory_id)
            self.assertEqual(repeated.version, 2)
            self.assertEqual(repeated.status, MemoryStatus.ACTIVE)
            with store.read() as connection:
                count = connection.execute(
                    "SELECT COUNT(*) AS count FROM memory_items WHERE workspace_id=? AND project_id=?",
                    (WORKSPACE_ID, PROJECT_ID),
                ).fetchone()["count"]
            self.assertEqual(count, 2)
            self.assertEqual(store.get_memory_record(old_result.memory_id).status, MemoryStatus.SUPERSEDED)
            self.assertEqual(store.get_memory_record(new_result.memory_id).status, MemoryStatus.ACTIVE)


if __name__ == "__main__":
    unittest.main()
