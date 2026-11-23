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


CONVERSATION_ID = "conv_consolidation_test"


def _row(sequence: int, role: str, text: str, timestamp: str) -> dict:
    return {
        "conversation_id": CONVERSATION_ID,
        "message_id": f"cm{sequence:03d}",
        "role": role,
        "timestamp": timestamp,
        "sequence": sequence,
        "text": text,
        "attachments": [],
        "metadata": {"topic_key": "consolidation"},
    }


def _records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _hash_tree(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _plan(root: Path) -> tuple[dict, Path, Path]:
    root.mkdir(parents=True, exist_ok=True)
    input_root = root / "input"
    input_root.mkdir()
    input_path = input_root / "consolidation.jsonl"
    rows = [
        _row(0, "user", "必须禁止 database_skill 直接包装 scripts/ 下的旧脚本。", "2026-11-21T13:00:00+08:00"),
        _row(1, "user", "禁止 database_skill 包装 scripts/ 旧脚本并调用 memory_core。", "2026-11-21T13:01:00+08:00"),
        _row(2, "user", "在计划中补上 profile attach 契约后，V1.2 就可以正式冻结并进入实现。", "2026-11-21T13:02:00+08:00"),
        _row(3, "user", "不要留模糊地带。", "2026-11-21T13:03:00+08:00"),
        _row(4, "user", "继续", "2026-11-21T15:20:00+08:00"),
        _row(5, "assistant", "继续是指继续完善规划，还是开始实现？", "2026-11-21T15:20:05+08:00"),
    ]
    input_path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    workspace = root / "workspace"
    plan = clean_plan(
        input_root=input_root,
        input_path=input_path,
        workspace_root=workspace,
        workspace_id="ws_consolidation",
        project_id="project_consolidation",
        source_id="src_consolidation",
        domain_profile="software_architecture",
        profile_version="1.0.0",
        conversation_ids=[CONVERSATION_ID],
        boundary_type="explicit_scan_boundary",
        boundary_evidence="explicit consolidation regression test",
    )
    return plan, workspace, input_path


def _apply_rows(root: Path, rows: list[dict], *, slug: str, domain_profile: str = "infrastructure") -> Path:
    input_root = root / "input"
    input_root.mkdir(parents=True)
    input_path = input_root / f"{slug}.jsonl"
    input_path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    plan = clean_plan(
        input_root=input_root,
        input_path=input_path,
        workspace_root=root / "workspace",
        workspace_id=f"ws_{slug}",
        project_id=f"project_{slug}",
        source_id=f"src_{slug}",
        domain_profile=domain_profile,
        profile_version="1.0.0",
        conversation_ids=[CONVERSATION_ID],
        boundary_type="explicit_scan_boundary",
        boundary_evidence=f"explicit {slug} regression test",
    )
    applied = clean_apply(plan, confirmation_token=plan["confirmation_token"])
    if applied["status"] != "applied":
        raise AssertionError(applied)
    return Path(applied["run_dir"])


class ConsolidationTests(unittest.TestCase):
    def test_group_task_noise_topic_merge_audit_and_idempotency(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            plan, workspace, _ = _plan(root)
            self.assertEqual(plan["status"], "planned")
            self.assertEqual(plan["counts"]["raw_candidates"], 4)
            self.assertEqual(plan["counts"]["consolidated_candidates"], 1)
            self.assertEqual(plan["counts"]["task_items"], 1)
            self.assertEqual(plan["counts"]["noise_items"], 1)
            self.assertGreaterEqual(plan["counts"]["suppressed_items"], 2)
            self.assertEqual(plan["counts"]["topic_segments"], 1)

            plan_again, _, _ = _plan(root / "again")
            group = plan["consolidation_summary"]
            group_again = plan_again["consolidation_summary"]
            self.assertEqual(group, group_again, "group identity must be stable for identical input")

            applied = clean_apply(plan, confirmation_token=plan["confirmation_token"])
            self.assertEqual(applied["status"], "applied")
            run_dir = Path(applied["run_dir"])
            raw_candidates = _records(run_dir / "raw_candidates.jsonl")
            groups = _records(run_dir / "candidates.jsonl")
            tasks = _records(run_dir / "task_history.jsonl")
            noise = _records(run_dir / "noise_items.jsonl")
            segments = _records(run_dir / "segments.jsonl")
            suppressed = _records(run_dir / "suppressed_candidates.jsonl")
            bundle = _records(run_dir / "review_bundle.jsonl")

            self.assertEqual(len(raw_candidates), 4)
            self.assertEqual(len(groups), 1)
            self.assertEqual(len(groups[0]["member_candidate_ids"]), 2)
            self.assertTrue(groups[0]["merge_reasons"])
            raw_evidence_ids = {
                evidence["evidence_id"]
                for candidate in raw_candidates
                for evidence in candidate["evidence"]
            }
            group_evidence_ids = {evidence["evidence_id"] for evidence in groups[0]["evidence"]}
            self.assertTrue(group_evidence_ids.issubset(raw_evidence_ids))
            self.assertGreaterEqual(len(group_evidence_ids), 2)
            self.assertFalse(groups[0]["promotion_eligible"])

            self.assertEqual(len(tasks), 1)
            self.assertEqual(tasks[0]["task_state"], "open")
            self.assertTrue(tasks[0]["not_long_term_candidate"])
            self.assertFalse(tasks[0]["promotion_eligible"])
            self.assertNotIn(tasks[0]["source_candidate_id"], groups[0]["member_candidate_ids"])

            self.assertEqual(len(noise), 1)
            self.assertEqual(noise[0]["reason"], "short_context_dependent_fragment")
            self.assertFalse(noise[0]["full_text_included"])
            self.assertNotIn(noise[0]["source_candidate_id"], groups[0]["member_candidate_ids"])

            self.assertEqual(len(segments), 1)
            self.assertTrue(segments[0]["merged_segment_ids"])
            self.assertIn("trivial_tail_merged", segments[0]["boundary_reasons"])
            self.assertTrue(segments[0]["merge_audit"])
            self.assertIn("time_gap_exceeded", segments[0]["merge_audit"][0]["original_boundary_reasons"])

            self.assertTrue(any(item["reason"] == "task_routed" for item in suppressed))
            self.assertTrue(any(item["reason"] == "noise_filtered" for item in suppressed))
            for item in suppressed:
                self.assertTrue(item["summary"])
                self.assertTrue(item["reason"])
                self.assertTrue(item["source_message_id"])
                self.assertFalse(item["full_text_included"])

            item_types = {item["item_type"] for item in bundle}
            self.assertEqual(item_types, {"grouped_candidate", "task_item", "noise_item"})
            self.assertTrue(all(item["suggestion_only"] for item in bundle))

            raw_hash = hashlib.sha256((run_dir / "raw_candidates.jsonl").read_bytes()).hexdigest()
            before = _hash_tree(run_dir)
            repeated = clean_apply(plan, confirmation_token=plan["confirmation_token"])
            self.assertEqual(repeated["status"], "already_applied")
            self.assertFalse(repeated["changed"])
            self.assertEqual(before, _hash_tree(run_dir))
            self.assertEqual(raw_hash, hashlib.sha256((run_dir / "raw_candidates.jsonl").read_bytes()).hexdigest())

    def test_consolidation_remains_read_only_outside_isolated_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            plan, workspace, _ = _plan(root)
            production = workspace / "production"
            production.mkdir(parents=True)
            sentinel = production / "canonical.sqlite3"
            sentinel.write_bytes(b"production-placeholder")
            before = hashlib.sha256(sentinel.read_bytes()).hexdigest()
            applied = clean_apply(plan, confirmation_token=plan["confirmation_token"])
            self.assertEqual(applied["status"], "applied")
            self.assertEqual(before, hashlib.sha256(sentinel.read_bytes()).hexdigest())
            self.assertEqual(len(list(workspace.rglob("canonical.sqlite3"))), 1)

    def test_group_synthesis_lifecycle_parent_and_decision_origin(self) -> None:
        rows = [
            _row(0, "user", "必须定义 memory_core.skill_api 作为 database_skill 唯一允许导入边界。", "2026-11-21T16:00:00+08:00"),
            _row(1, "user", "database_skill 禁止直接导入 memory_core 的 scripts/ 旧脚本。", "2026-11-21T16:01:00+08:00"),
            _row(2, "user", "database_skill 禁止直接包装 memory_core 的 scripts/ 旧脚本。", "2026-11-21T16:02:00+08:00"),
            _row(3, "user", "scripts 中的通用逻辑必须下沉到 src/memory_core。", "2026-11-21T16:03:00+08:00"),
            _row(4, "user", "### P1 工程细节建议\n* 解决独立包与 memory_core 的依赖定位：如果 database_skill 与 memory_core 分开存放，不能靠相对路径 sys.path 硬拼。", "2026-11-21T16:04:00+08:00"),
            _row(5, "user", "把 profile attach 独立出来后，命令语义会彻底干净：\n* 完全赞同新增 profile attach 作为总体统一入口。\n* real/attach 和 milvus attach 统一走 profile attach 命令。\n* profile attach 只负责接已有目标，并且必须保持零数据库副作用。\n* init 只负责创建新 canonical SQLite 库。", "2026-11-21T16:05:00+08:00"),
            _row(6, "user", "拍板：Milvus V1 只允许只读操作，禁止 canonical 写入。", "2026-11-21T16:06:00+08:00"),
        ]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            input_root = root / "input"
            input_root.mkdir()
            input_path = input_root / "synthesis.jsonl"
            input_path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
            plan = clean_plan(
                input_root=input_root,
                input_path=input_path,
                workspace_root=root / "workspace",
                workspace_id="ws_synthesis",
                project_id="project_synthesis",
                source_id="src_synthesis",
                domain_profile="software_architecture",
                profile_version="1.0.0",
                conversation_ids=[CONVERSATION_ID],
                boundary_type="explicit_scan_boundary",
                boundary_evidence="explicit synthesis regression test",
            )
            applied = clean_apply(plan, confirmation_token=plan["confirmation_token"])
            self.assertEqual(applied["status"], "applied")
            run_dir = Path(applied["run_dir"])
            raw = _records(run_dir / "raw_candidates.jsonl")
            groups = _records(run_dir / "candidates.jsonl")
            parents = _records(run_dir / "parent_groups.jsonl")
            bundle = _records(run_dir / "review_bundle.jsonl")
            stats = json.loads((run_dir / "review_stats.json").read_text(encoding="utf-8"))

            self.assertTrue(all("共享结构父组" not in parent["content"] for parent in parents))
            self.assertEqual(stats["consolidated_group_count"], len(groups))
            self.assertEqual(
                stats["top_level_review_group_count"],
                stats["top_level_grouped_candidate_count"] + stats["top_level_parent_group_count"],
            )
            self.assertEqual(stats["nested_group_count"], stats["nested_child_group_count"])

            api_group = next(group for group in groups if "memory_core.skill_api" in group["content"] and len(group["member_candidate_ids"]) >= 3)
            self.assertIn("scripts", api_group["content"])
            self.assertIn("旧脚本", api_group["content"])
            self.assertIn("src/memory_core", api_group["content"])
            self.assertNotIn("依赖定位", api_group["content"])
            dependency_group = next(group for group in groups if "依赖定位" in group["content"])
            self.assertEqual(dependency_group["validation_status"], "standard")
            self.assertEqual(dependency_group["hierarchy_role"], "independent_group")
            self.assertNotIn("experimental_supporting_group", dependency_group["risk_flags"])
            self.assertFalse(dependency_group["promotion_eligible"])
            self.assertNotEqual(dependency_group["group_id"], api_group["group_id"])
            self.assertEqual(dependency_group["parent_group_ids"], [])
            self.assertFalse(any(dependency_group["group_id"] in parent["member_group_ids"] for parent in parents))

            profile_group = next(group for group in groups if "profile attach" in group["content"] and "real/attach" in group["content"])
            self.assertIn("新增 profile attach", profile_group["content"])
            self.assertIn("统一走 profile attach", profile_group["content"])

            milvus_group = next(group for group in groups if "Milvus V1" in group["content"])
            self.assertEqual(milvus_group["candidate_type"], "constraint")
            self.assertEqual(milvus_group["decision_origin"], "user_decision")

            profile_parent = next(
                parent for parent in parents
                if parent["parent_group_ids"]
                and "新增 profile attach" in parent["content"]
                and "只负责接已有目标" in parent["content"]
            )
            lifecycle = next(
                parent for parent in parents
                if not parent["parent_group_ids"]
                and profile_parent["parent_group_id"] in parent["member_parent_group_ids"]
            )
            self.assertIn(lifecycle["parent_type"], {"shared_context", "lifecycle"})
            self.assertIn("init", lifecycle["content"])
            self.assertIn("副作用", lifecycle["content"])
            lifecycle_item = next(item for item in bundle if item.get("parent_group_id") == lifecycle["parent_group_id"])
            nested_parent_ids = {item["parent_group_id"] for item in lifecycle_item["child_parent_groups"]}
            self.assertIn(profile_parent["parent_group_id"], nested_parent_ids)
            top_level_group_ids = {item["group_id"] for item in bundle if item["item_type"] == "grouped_candidate"}
            nested_profile_group_ids = {
                child["group_id"]
                for item in lifecycle_item["child_parent_groups"]
                for child in item["child_groups"]
            }
            self.assertFalse(nested_profile_group_ids & top_level_group_ids)

            raw_evidence_ids = {evidence["evidence_id"] for candidate in raw for evidence in candidate["evidence"]}
            for group in groups:
                self.assertTrue({evidence["evidence_id"] for evidence in group["evidence"]} <= raw_evidence_ids)

    def test_parent_groups_are_single_entries_and_security_domain_split(self) -> None:
        rows = [
            _row(0, "user", "### 安全规则\n* 上游严格执行字段白名单序列化；secret_guard 检测到 Canary 时标记 blocked 并拒绝回显。", "2026-11-21T17:00:00+08:00"),
            _row(1, "user", "### 回执规则\n* 任何 blocked 必须落盘最小安全回执。", "2026-11-21T17:01:00+08:00"),
            _row(2, "user", "### 术语规则\n* 此外，产品原始 TXT 建议统一改为原始业务语料文件。", "2026-11-21T17:02:00+08:00"),
            _row(3, "user", "### 只读规则\n* 拍板：Milvus V1 只允许只读操作，禁止 canonical 写入。", "2026-11-21T17:03:00+08:00"),
        ]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            input_root = root / "input"
            input_root.mkdir()
            input_path = input_root / "security_split.jsonl"
            input_path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
            plan = clean_plan(
                input_root=input_root,
                input_path=input_path,
                workspace_root=root / "workspace",
                workspace_id="ws_security",
                project_id="project_security",
                source_id="src_security",
                domain_profile="software_architecture",
                profile_version="1.0.0",
                conversation_ids=[CONVERSATION_ID],
                boundary_type="explicit_scan_boundary",
                boundary_evidence="explicit security hierarchy test",
            )
            applied = clean_apply(plan, confirmation_token=plan["confirmation_token"])
            self.assertEqual(applied["status"], "applied")
            run_dir = Path(applied["run_dir"])
            bundle = _records(run_dir / "review_bundle.jsonl")
            stats = json.loads((run_dir / "review_stats.json").read_text(encoding="utf-8"))
            parent_items = [item for item in bundle if item["item_type"] == "parent_group"]
            grouped_items = [item for item in bundle if item["item_type"] == "grouped_candidate"]
            top_level_group_ids = {item["group_id"] for item in grouped_items}

            security_parent = next(
                item for item in parent_items
                if "白名单" in item["content"] and "blocked" in item["content"] and "安全回执" in item["content"]
            )
            nested_ids = {child["group_id"] for child in security_parent["child_groups"]}
            self.assertFalse(nested_ids & top_level_group_ids)

            domain_items = [item for item in grouped_items if "原始业务语料" in item["content"]]
            self.assertEqual(len(domain_items), 1)
            self.assertNotIn(domain_items[0]["group_id"], nested_ids)

            milvus_items = [item for item in grouped_items if "Milvus V1" in item["content"]]
            self.assertEqual(len(milvus_items), 1)
            self.assertEqual(milvus_items[0]["decision_origin"], "user_decision")
            self.assertEqual(milvus_items[0]["candidate_type"], "constraint")

            self.assertEqual(
                stats["top_level_review_group_count"],
                len(parent_items) + len(grouped_items),
            )
    def test_task_states_and_noise_preservation(self) -> None:
        rows = [
            _row(0, "user", "请补上配置契约，完成后进入实现。", "2026-11-21T18:00:00+08:00"),
            _row(1, "assistant", "已完成配置契约更新。", "2026-11-21T18:01:00+08:00"),
            _row(2, "user", "确认，继续进入实现。", "2026-11-21T18:02:00+08:00"),
            _row(3, "assistant", "PROPOSAL: 补上安全回执后进入实现。", "2026-11-21T18:03:00+08:00"),
            _row(4, "user", "更新 Milvus 模式说明，明确二选一：要么只读，要么可写。", "2026-11-21T18:04:00+08:00"),
            _row(5, "user", "DECISION: Milvus 采用只读方案，禁止写入。", "2026-11-21T18:05:00+08:00"),
            _row(6, "user", "如果在日志中出现真实 key，说明字符串替换可能漏掉变体。", "2026-11-21T18:06:00+08:00"),
            _row(7, "user", "不要留模糊地带。", "2026-11-21T18:07:00+08:00"),
        ]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            input_root = root / "input"
            input_root.mkdir()
            input_path = input_root / "task_state.jsonl"
            input_path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
            plan = clean_plan(
                input_root=input_root,
                input_path=input_path,
                workspace_root=root / "workspace",
                workspace_id="ws_task",
                project_id="project_task",
                source_id="src_task",
                domain_profile="software_architecture",
                profile_version="1.0.0",
                conversation_ids=[CONVERSATION_ID],
                boundary_type="explicit_scan_boundary",
                boundary_evidence="explicit task-state test",
            )
            applied = clean_apply(plan, confirmation_token=plan["confirmation_token"])
            self.assertEqual(applied["status"], "applied")
            run_dir = Path(applied["run_dir"])
            tasks = _records(run_dir / "task_history.jsonl")
            noise = _records(run_dir / "noise_items.jsonl")

            completed = next(task for task in tasks if "配置契约" in task["content"])
            self.assertEqual(completed["task_state"], "completed")
            self.assertTrue(completed["archived"])
            self.assertTrue(completed["confirmed_by_message_id"])

            assistant_only = next(task for task in tasks if "安全回执" in task["content"])
            self.assertEqual(assistant_only["task_state"], "needs_user_confirmation")
            self.assertFalse(assistant_only["archived"])

            resolved = next(task for task in tasks if "二选一" in task["content"])
            self.assertEqual(resolved["task_state"], "resolved_to_constraint")
            self.assertTrue(resolved["archived"])
            self.assertTrue(resolved["resolution_candidate_id"])

            self.assertFalse(any("字符串替换" in task["content"] for task in tasks))
            noise_item = next(item for item in noise if "不要留模糊地带" in item["summary"])
            self.assertEqual(noise_item["suggested_action"], "inspect_then_discard")
            self.assertFalse(noise_item["full_text_included"])
    def test_contextual_supporting_constraint_remains_experimental(self) -> None:
        rows = [
            _row(0, "user", "必须定义 orch_api 作为唯一导入边界，并通过 shared_lib 暴露通用能力。", "2026-11-21T19:00:00+08:00"),
            _row(1, "user", "必须禁止绕过 orch_api 直接调用 internal_scripts，通用流程统一由 shared_lib 提供。", "2026-11-21T19:01:00+08:00"),
            _row(2, "user", "### P1 工程细节建议\n* 该补充项必须在 orch_api 与 shared_lib 内实现，方案稍后细化。", "2026-11-21T19:02:00+08:00"),
        ]
        with tempfile.TemporaryDirectory() as temp:
            run_dir = _apply_rows(Path(temp), rows, slug="experimental")
            groups = _records(run_dir / "candidates.jsonl")
            experimental = next(group for group in groups if group["validation_status"] == "needs_more_real_domain_validation")
            self.assertEqual(experimental["suggested_action"], "review_supporting_evidence_no_promotion")
            self.assertIn("experimental_supporting_group", experimental["risk_flags"])
            self.assertFalse(experimental["promotion_eligible"])
            self.assertTrue(experimental["evidence"])

    def test_short_restatement_merges_and_independent_table_constraint_stays_standard(self) -> None:
        rows = [
            _row(
                0,
                "user",
                "### P0 boundary\n"
                "* 必须在 orch_api 中提供唯一导入入口，并禁止直接调用 internal_scripts。\n"
                "* 禁止直接包装 internal_scripts 中带硬编码的旧命令，所有通用能力必须通过 shared_lib 暴露，并只能从 orch_api 调用。\n"
                "* 不得把复用能力变成包装 internal_scripts。",
                "2026-11-21T20:00:00+08:00",
            ),
            _row(
                1,
                "user",
                "### P1 工程细节\n| 1 | Dependency placement | 独立包必须通过可安装的 shared_lib 版本契约定位，不能靠相对路径硬拼。 |",
                "2026-11-21T20:01:00+08:00",
            ),
        ]
        with tempfile.TemporaryDirectory() as temp:
            run_dir = _apply_rows(Path(temp), rows, slug="restatement")
            raw = _records(run_dir / "raw_candidates.jsonl")
            groups = _records(run_dir / "candidates.jsonl")
            api_group = next(group for group in groups if "orch_api" in group["content"] and "internal_scripts" in group["content"])
            self.assertGreaterEqual(len(api_group["member_candidate_ids"]), 3)
            self.assertIn("same-source short restatement", api_group["merge_reasons"])
            self.assertIn("orch_api", api_group["content"])
            self.assertIn("internal_scripts", api_group["content"])
            self.assertIn("shared_lib", api_group["content"])

            dependency_group = next(group for group in groups if "版本契约" in group["content"])
            self.assertEqual(dependency_group["validation_status"], "standard")
            self.assertIn(dependency_group["hierarchy_role"], {"independent_group", "group_leaf"})
            self.assertNotIn("experimental_supporting_group", dependency_group["risk_flags"])
            self.assertFalse(dependency_group["promotion_eligible"])

            raw_evidence_ids = {evidence["evidence_id"] for candidate in raw for evidence in candidate["evidence"]}
            self.assertTrue({evidence["evidence_id"] for evidence in api_group["evidence"]} <= raw_evidence_ids)

    def test_one_generic_rare_phrase_does_not_merge_unrelated_constraints(self) -> None:
        rows = [
            _row(0, "user", "要求只需要保留核心配置，旧文件必须归档。", "2026-11-21T20:30:00+08:00"),
            _row(1, "user", "要求只需要记录最终状态，临时数据必须清理。", "2026-11-21T20:31:00+08:00"),
        ]
        with tempfile.TemporaryDirectory() as temp:
            run_dir = _apply_rows(Path(temp), rows, slug="rare_phrase")
            groups = _records(run_dir / "candidates.jsonl")
            self.assertEqual(len(groups), 2)
            self.assertTrue(all(group["member_count"] == 1 for group in groups))

    def test_independent_supporting_decision_is_not_experimental(self) -> None:
        rows = [
            _row(0, "user", "DECISION: component_x 和 schema_y 采用统一入口。", "2026-11-21T21:10:00+08:00"),
            _row(1, "user", "DECISION: component_x 和 schema_y 保留扩展点。", "2026-11-21T21:11:00+08:00"),
            _row(2, "user", "### P1 工程细节\n* component_x 和 schema_y 命名统一使用 item_version，不再混用 version。", "2026-11-21T21:12:00+08:00"),
        ]
        with tempfile.TemporaryDirectory() as temp:
            run_dir = _apply_rows(Path(temp), rows, slug="independent_decision")
            groups = _records(run_dir / "candidates.jsonl")
            independent = next(group for group in groups if "item_version" in group["content"])
            self.assertEqual(independent["validation_status"], "standard")
            self.assertEqual(independent["hierarchy_role"], "independent_group")
            self.assertNotIn("experimental_supporting_group", independent["risk_flags"])
            self.assertFalse(independent["promotion_eligible"])

    def test_choice_task_resolves_to_best_matching_later_decision(self) -> None:
        rows = [
            _row(0, "user", "更新方案：需要在 alpha_api 与 beta_store 之间二选一并写死，完成后进入实现。", "2026-11-21T21:30:00+08:00"),
            _row(1, "user", "DECISION: 只读模式需要保留 init 结构。", "2026-11-21T21:31:00+08:00"),
            _row(2, "user", "DECISION: alpha_api 采用只读，beta_store 禁止写入。", "2026-11-21T21:32:00+08:00"),
        ]
        with tempfile.TemporaryDirectory() as temp:
            run_dir = _apply_rows(Path(temp), rows, slug="resolution")
            tasks = _records(run_dir / "task_history.jsonl")
            resolved = next(task for task in tasks if "二选一" in task["content"])
            self.assertEqual(resolved["task_state"], "resolved_to_constraint")
            self.assertEqual(resolved["resolution_message_id"], "cm002")
            raw = {item["candidate_id"]: item for item in _records(run_dir / "raw_candidates.jsonl")}
            self.assertIn("alpha_api", raw[resolved["resolution_candidate_id"]]["content"])

    def test_review_bundle_displays_each_parent_and_group_once(self) -> None:
        rows = [
            _row(0, "user", "### lifecycle rules\n* init 只负责创建新存储。\n* attach 只负责接入已有目标，必须保持零副作用。", "2026-11-21T21:00:00+08:00"),
            _row(1, "user", "### attach entry\n* 采用 attach 作为所有已有目标的统一入口。\n* sqlite attach 和 remote index attach 都通过 attach 执行。", "2026-11-21T21:01:00+08:00"),
        ]
        with tempfile.TemporaryDirectory() as temp:
            run_dir = _apply_rows(Path(temp), rows, slug="bundle_once")
            bundle = _records(run_dir / "review_bundle.jsonl")
            stats = json.loads((run_dir / "review_stats.json").read_text(encoding="utf-8"))
            top_parents = [item for item in bundle if item["item_type"] == "parent_group"]
            top_groups = [item for item in bundle if item["item_type"] == "grouped_candidate"]
            top_group_ids = [item["group_id"] for item in top_groups]
            self.assertEqual(len(top_group_ids), len(set(top_group_ids)))

            nested_group_ids: list[str] = []
            nested_parent_ids: list[str] = []
            def walk(item: dict) -> None:
                for child in item.get("child_groups", []):
                    nested_group_ids.append(child["group_id"])
                for child_parent in item.get("child_parent_groups", []):
                    nested_parent_ids.append(child_parent["parent_group_id"])
                    walk(child_parent)
            for parent in top_parents:
                walk(parent)
            self.assertEqual(len(nested_group_ids), len(set(nested_group_ids)))
            self.assertEqual(len(nested_parent_ids), len(set(nested_parent_ids)))
            self.assertFalse(set(top_group_ids) & set(nested_group_ids))
            self.assertEqual(stats["top_level_grouped_candidate_count"], len(top_groups))
            self.assertEqual(stats["nested_group_count"], len(nested_group_ids))
            self.assertEqual(stats["candidate_count"], stats["top_level_grouped_candidate_count"])
            markdown = (run_dir / "REVIEW_BUNDLE.md").read_text(encoding="utf-8")
            for label in ("raw candidates", "consolidated groups", "top-level grouped candidates", "nested grouped candidates"):
                self.assertIn(label, markdown)


if __name__ == "__main__":
    unittest.main()