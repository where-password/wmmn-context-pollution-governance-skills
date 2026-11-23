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

from conversation_cleaner.api_v1 import clean_apply, clean_plan


FIXTURE_ROOT = SKILL_ROOT / "assets" / "synthetic"


def _records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _plan_and_apply(root: Path, *, fixture: str, conversation_ids: list[str], domain_profile: str) -> Path:
    input_root = FIXTURE_ROOT
    workspace = root / "workspace"
    plan = clean_plan(
        input_root=input_root,
        input_path=FIXTURE_ROOT / fixture,
        workspace_root=workspace,
        workspace_id=f"ws_{domain_profile}",
        project_id=f"project_{domain_profile}",
        source_id=f"src_{domain_profile}",
        domain_profile=domain_profile,
        profile_version="1.0.0",
        conversation_ids=conversation_ids,
        boundary_type="explicit_scan_boundary",
        boundary_evidence="explicit cross-domain generalization test",
    )
    applied = clean_apply(plan, confirmation_token=plan["confirmation_token"])
    if applied["status"] != "applied":
        raise AssertionError(applied)
    return Path(applied["run_dir"])


class GeneralizationTests(unittest.TestCase):
    def test_two_synthetic_domains_do_not_cross_contaminate(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            software_run = _plan_and_apply(
                root / "software",
                fixture="controlled_conversation.jsonl",
                conversation_ids=["conv_synthetic_001"],
                domain_profile="software_architecture",
            )
            cross_run = _plan_and_apply(
                root / "customer",
                fixture="cross_domain_conversations.jsonl",
                conversation_ids=["conv_support_ops_001", "conv_sales_ops_002"],
                domain_profile="customer_operations",
            )
            software_groups = _records(software_run / "candidates.jsonl")
            cross_groups = _records(cross_run / "candidates.jsonl")
            cross_raw = _records(cross_run / "raw_candidates.jsonl")
            unit_to_conversation = {
                evidence["source_unit_id"]: candidate["conversation_id"]
                for candidate in cross_raw
                for evidence in candidate["evidence"]
            }

            self.assertTrue({group["group_id"] for group in software_groups}.isdisjoint({group["group_id"] for group in cross_groups}))
            observed_conversations: set[str] = set()
            for group in cross_groups:
                conversations = {
                    unit_to_conversation[evidence["source_unit_id"]]
                    for evidence in group["evidence"]
                    if evidence["source_unit_id"] in unit_to_conversation
                }
                self.assertEqual(len(conversations), 1, group)
                observed_conversations.update(conversations)
            self.assertEqual(observed_conversations, {"conv_support_ops_001", "conv_sales_ops_002"})


if __name__ == "__main__":
    unittest.main()