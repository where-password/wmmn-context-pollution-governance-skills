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

from conversation_cleaner.api_v1 import clean_apply, clean_plan
from conversation_cleaner.extraction import _classify_heuristic


CONVERSATION_ID = "conv_heuristic_test"


def _row(sequence: int, role: str, text: str) -> dict:
    return {
        "conversation_id": CONVERSATION_ID,
        "message_id": f"hm{sequence:03d}",
        "role": role,
        "timestamp": f"2026-11-21T12:{sequence:02d}:00+08:00",
        "sequence": sequence,
        "text": text,
        "attachments": [],
        "metadata": {"topic_key": "heuristic"},
    }


def _write_input(root: Path, rows: list[dict]) -> tuple[Path, Path]:
    input_root = root / "input"
    input_root.mkdir()
    input_path = input_root / "heuristic.jsonl"
    input_path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    return input_root, input_path


def _plan(
    root: Path,
    rows: list[dict],
    *,
    per_message_cap: int = 30,
    total_cap: int = 100,
) -> tuple[dict, Path]:
    input_root, input_path = _write_input(root, rows)
    workspace = root / "workspace"
    plan = clean_plan(
        input_root=input_root,
        input_path=input_path,
        workspace_root=workspace,
        workspace_id="ws_heuristic",
        project_id="project_heuristic",
        source_id="src_heuristic",
        domain_profile="software_architecture",
        profile_version="1.0.0",
        conversation_ids=[CONVERSATION_ID],
        boundary_type="explicit_scan_boundary",
        boundary_evidence="explicit heuristic regression test",
        heuristic_per_message_cap=per_message_cap,
        heuristic_total_cap=total_cap,
    )
    return plan, workspace


def _records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _hash_tree(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            result[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


class HeuristicExtractionTests(unittest.TestCase):
    def test_markdown_user_requirements_generate_verified_review_candidates(self) -> None:
        text = (
            "### 契约\\\n"
            "\\\n"
            "\\* \\*\\*必须保留\\*\\* canonical 不变。\\\n"
            "\\* \\*\\*采用\\*\\* profile attach 作为独立入口。\\\n"
            "\\* \\*\\*优先\\*\\* 拆分只读接口，确保公开边界保持最小。\\\n"
            "\\* \\*\\*不要删除\\*\\* 审计记录。"
        )
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            plan, workspace = _plan(root, [_row(0, "user", text)])
            self.assertEqual(plan["status"], "planned")
            self.assertGreaterEqual(plan["counts"]["candidates"], 4)
            self.assertEqual(plan["extractor_summary"]["explicit_candidate_count"], 0)
            self.assertGreaterEqual(plan["extractor_summary"]["heuristic_candidate_count"], 4)

            applied = clean_apply(plan, confirmation_token=plan["confirmation_token"])
            self.assertEqual(applied["status"], "applied")
            run_dir = Path(applied["run_dir"])
            candidates = _records(run_dir / "candidates.jsonl")
            raw_candidates = _records(run_dir / "raw_candidates.jsonl")
            messages = {item["source_unit_id"]: item for item in _records(run_dir / "messages.jsonl")}

            self.assertTrue(all(item["verification_state"] == "quote_verified" for item in raw_candidates))
            self.assertTrue(all(item["review_status"] == "pending_review" for item in candidates))
            self.assertTrue(all("manual_review_required" in item["risk_flags"] for item in candidates))
            self.assertTrue(all(item["promotion_eligible"] is False for item in candidates))
            self.assertTrue(all(item["extraction_method"] == "heuristic_fallback" for item in candidates))
            self.assertIn("constraint", {item["candidate_type"] for item in candidates})
            self.assertIn("decision", {item["candidate_type"] for item in candidates})
            self.assertIn("preference", {item["candidate_type"] for item in candidates})

            for candidate in candidates:
                evidence = candidate["evidence"][0]
                source = messages[evidence["source_unit_id"]]
                self.assertEqual(
                    source["text"][evidence["char_start"]:evidence["char_end"]],
                    evidence["quote"],
                )
                self.assertTrue(evidence["verified"])

            first_hashes = _hash_tree(run_dir)
            repeated = clean_apply(plan, confirmation_token=plan["confirmation_token"])
            self.assertEqual(repeated["status"], "already_applied")
            self.assertFalse(repeated["changed"])
            self.assertEqual(first_hashes, _hash_tree(run_dir))

    def test_assistant_plain_text_questions_and_short_noise_do_not_create_candidates(self) -> None:
        rows = [
            _row(0, "assistant", "我认为系统必须删除所有旧索引，这是明显的事实。"),
            _row(1, "user", "我们是否必须删除旧索引？"),
            _row(2, "user", "继续"),
            _row(3, "user", "可能需要调整 profile。"),
            _row(4, "user", "这个方案需要保留，还有没有备用槽位"),
            _row(5, "user", "第一个问题，这个方案需要保留，还是应该替换"),
        ]
        with tempfile.TemporaryDirectory() as temp:
            plan, _ = _plan(Path(temp), rows)
            self.assertEqual(plan["status"], "planned")
            self.assertEqual(plan["counts"]["candidates"], 0)
            self.assertEqual(plan["counts"]["open_questions"], 0)
            self.assertGreaterEqual(plan["extractor_summary"]["questionish_skipped"], 4)
            self.assertIn("no explicit markers or conservative heuristic candidates were found", plan["warnings"])

    def test_weak_only_can_phrase_does_not_override_preference(self) -> None:
        text = "我对于 AI 层面相对了解，专业只能说基础认知，我可以继续学习，所以希望侧重专业基础并扩大 AI 优势。"
        self.assertEqual(_classify_heuristic(text), ("preference", 2))

    def test_numbered_questions_do_not_suppress_declarative_requirements(self) -> None:
        rows = [
            _row(0, "user", "第二个问题，项目需要一个可用的虚构背景，例如某公司，不能只写某企业。"),
            _row(1, "user", "第三个问题，我希望侧重专业基础，并扩大 AI 优势。"),
            _row(2, "user", "第一个问题，这个方案需要保留，还是应该替换？"),
            _row(3, "user", "但 10.2 节允许运行，14.2 节又写恢复后再生成，二者冲突。"),
            _row(4, "user", "前序决定尚未同步进本规划。"),
            _row(5, "user", "采用 A 方案，但保留 B 方案。"),
        ]
        with tempfile.TemporaryDirectory() as temp:
            plan, _ = _plan(Path(temp), rows)
            self.assertEqual(plan["status"], "planned")
            self.assertEqual(plan["counts"]["candidates"], 3)
            self.assertGreaterEqual(plan["extractor_summary"]["questionish_skipped"], 2)

    def test_mixed_signal_claim_is_marked_for_split_review(self) -> None:
        text = "先这样，我有时间和老师聊一下先决定，这个是初版，我选题就是这个了，然后不要写入工作区，我会单独开一个工作来完成，你只需要给我目前三个填写就够了"
        with tempfile.TemporaryDirectory() as temp:
            plan, _ = _plan(Path(temp), [_row(0, "user", text)])
            self.assertEqual(plan["status"], "planned")
            applied = clean_apply(plan, confirmation_token=plan["confirmation_token"])
            self.assertEqual(applied["status"], "applied")
            run_dir = Path(applied["run_dir"])
            groups = _records(run_dir / "candidates.jsonl")
            self.assertEqual(len(groups), 1)
            self.assertIn("mixed_signal_claim", groups[0]["risk_flags"])
            self.assertEqual(groups[0]["suggested_action"], "split_review")

    def test_structured_long_message_over_five_decisions_is_not_silently_capped(self) -> None:
        text = "\n".join(f"{index}. 决定采用方案{index}，并保留对应审计记录。" for index in range(1, 9))
        with tempfile.TemporaryDirectory() as temp:
            plan, _ = _plan(Path(temp), [_row(0, "user", text)])
            self.assertEqual(plan["status"], "planned")
            self.assertGreaterEqual(plan["counts"]["raw_candidates"], 8)
            self.assertEqual(plan["extractor_summary"]["heuristic_limit_suppressed"], 0)
            self.assertGreaterEqual(plan["extractor_summary"]["max_heuristic_per_message"], 20)
            applied = clean_apply(plan, confirmation_token=plan["confirmation_token"])
            self.assertEqual(applied["status"], "applied")
            raw_candidates = _records(Path(applied["run_dir"]) / "raw_candidates.jsonl")
            self.assertGreaterEqual(len(raw_candidates), 8)
            self.assertTrue(any(item["structure_kind"] == "numbered_item" for item in raw_candidates))

    def test_configurable_cap_records_every_suppressed_item(self) -> None:
        text = "\n".join(f"{index}. 必须保留规则{index}并记录来源。" for index in range(1, 26))
        with tempfile.TemporaryDirectory() as temp:
            plan, _ = _plan(Path(temp), [_row(0, "user", text)], per_message_cap=5, total_cap=100)
            self.assertEqual(plan["status"], "planned")
            self.assertEqual(plan["counts"]["raw_candidates"], 5)
            self.assertEqual(plan["extractor_summary"]["max_heuristic_per_message"], 5)
            self.assertEqual(plan["extractor_summary"]["heuristic_limit_suppressed"], 20)
            applied = clean_apply(plan, confirmation_token=plan["confirmation_token"])
            self.assertEqual(applied["status"], "applied")
            suppressed = _records(Path(applied["run_dir"]) / "suppressed_candidates.jsonl")
            cap_items = [item for item in suppressed if item["reason"] == "per_message_cap"]
            self.assertEqual(len(cap_items), 20)
            self.assertTrue(all(item["summary"] and item["source_message_id"] for item in cap_items))
            self.assertTrue(all(item["full_text_included"] is False for item in cap_items))


if __name__ == "__main__":
    unittest.main()
