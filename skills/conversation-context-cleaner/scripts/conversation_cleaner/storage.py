from __future__ import annotations

import json
import os
import shutil
import uuid
from collections import Counter
from pathlib import Path
from typing import Any

from .consolidation import ConsolidationResult, GroupedCandidate, NoiseItem, TaskItem
from .contracts import (
    CONVERSATION_CONTRACT_VERSION,
    CONVERSATION_SKILL_API_VERSION,
    EXTRACTOR_VERSION,
    STATE_RELATIVE_ROOT,
    ExtractionResult,
    IncompleteRunError,
)
from .security import ensure_within, scan_texts_for_secrets
from .util import atomic_write_text, pretty_json, sha256_json, sha256_text, utc_now


RUN_ARTIFACTS = (
    "messages.jsonl",
    "segments.jsonl",
    "raw_candidates.jsonl",
    "candidates.jsonl",
    "parent_groups.jsonl",
    "task_history.jsonl",
    "noise_items.jsonl",
    "open_questions.jsonl",
    "suppressed_candidates.jsonl",
    "observations.jsonl",
    "state_transitions.jsonl",
    "state_review.jsonl",
    "review_bundle.jsonl",
    "review_stats.json",
    "REVIEW_BUNDLE.md",
    "audit_report.json",
)
EXPORT_FILES = (
    "raw_candidates.jsonl",
    "candidates.jsonl",
    "parent_groups.jsonl",
    "task_history.jsonl",
    "noise_items.jsonl",
    "open_questions.jsonl",
    "suppressed_candidates.jsonl",
    "observations.jsonl",
    "state_transitions.jsonl",
    "state_review.jsonl",
    "review_bundle.jsonl",
    "review_stats.json",
    "REVIEW_BUNDLE.md",
    "audit_report.json",
)


def state_root(workspace_root: Path) -> Path:
    return workspace_root / STATE_RELATIVE_ROOT


def run_directory(workspace_root: Path, run_id: str) -> Path:
    return state_root(workspace_root) / "runs" / run_id


def _jsonl(records: list[dict[str, Any]]) -> str:
    return "".join(json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n" for item in records)


def _evidence_summary(evidence: Any, *, quote_limit: int = 300) -> list[dict[str, Any]]:
    return [
        {
            "message_id": item.message_id,
            "source_unit_id": item.source_unit_id,
            "quote": item.quote[:quote_limit],
            "verified": item.verified,
            "status": item.status,
        }
        for item in evidence
    ]


def _parent_bundle_item(
    parent: Any,
    *,
    groups_by_id: dict[str, Any],
    parents_by_id: dict[str, Any],
    top_level: bool,
) -> dict[str, Any]:
    item = {
        "item_type": "parent_group",
        "bundle_item_id": "bundle_parent_" + sha256_text(parent.parent_group_id)[:24],
        "parent_group_id": parent.parent_group_id,
        "parent_type": parent.parent_type,
        "claim_key": parent.claim_key,
        "title": parent.title,
        "content": parent.content,
        "summary": parent.summary,
        "member_group_ids": list(parent.member_group_ids),
        "member_parent_group_ids": list(parent.member_parent_group_ids),
        "parent_group_ids": list(parent.parent_group_ids),
        "member_candidate_ids": list(parent.member_candidate_ids),
        "merge_reasons": list(parent.merge_reasons),
        "risks": list(parent.risk_flags),
        "child_groups": [groups_by_id[group_id].to_dict() for group_id in parent.member_group_ids if group_id in groups_by_id],
        "child_parent_groups": [
            _parent_bundle_item(
                parents_by_id[parent_id],
                groups_by_id=groups_by_id,
                parents_by_id=parents_by_id,
                top_level=False,
            )
            for parent_id in parent.member_parent_group_ids
            if parent_id in parents_by_id
        ],
        "evidence_summary": _evidence_summary(parent.evidence),
        "suggested_action": parent.suggested_action,
        "suggestion_only": True,
        "review_status": parent.review_status,
        "promotion_eligible": False,
        "top_level": top_level,
    }
    return item


def _review_bundle(extraction: ExtractionResult, consolidation: ConsolidationResult) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    groups_by_id = {group.group_id: group for group in consolidation.grouped_candidates}
    parents_by_id = {parent.parent_group_id: parent for parent in consolidation.parent_groups}
    nested_group_ids = {group_id for parent in consolidation.parent_groups for group_id in parent.member_group_ids}
    for parent in consolidation.parent_groups:
        if parent.parent_group_ids:
            continue
        items.append(
            _parent_bundle_item(
                parent,
                groups_by_id=groups_by_id,
                parents_by_id=parents_by_id,
                top_level=True,
            )
        )
    for group in consolidation.grouped_candidates:
        if group.group_id in nested_group_ids:
            continue
        items.append(
            {
                "item_type": "grouped_candidate",
                "bundle_item_id": "bundle_group_" + sha256_text(group.group_id)[:24],
                "group_id": group.group_id,
                "candidate_id": group.candidate_id,
                "claim_key": group.claim_key,
                "workspace_id": group.workspace_id,
                "project_id": group.project_id,
                "conversation_id": group.conversation_id,
                "topic_segment_id": group.topic_segment_id,
                "candidate_type": group.candidate_type,
                "content": group.content,
                "summary": group.summary,
                "member_candidate_ids": list(group.member_candidate_ids),
                "member_summaries": list(group.member_summaries),
                "child_claims": list(group.child_claims),
                "hierarchy_role": group.hierarchy_role,
                "merge_reasons": list(group.merge_reasons),
                "parent_group_ids": list(group.parent_group_ids),
                "decision_origin": group.decision_origin,
                "overlap_with": list(group.overlap_with),
                "risks": list(group.risk_flags),
                "evidence_summary": _evidence_summary(group.evidence),
                "suggested_action": group.suggested_action,
                "suggestion_only": True,
                "review_status": group.review_status,
                "record_status": group.record_status,
                "extraction_method": group.extraction_method,
                "extraction_methods": list(group.extraction_methods),
                "heuristic_rule": group.heuristic_rule,
                "promotion_eligible": False,
            }
        )
    for task in consolidation.task_items:
        items.append(
            {
                "item_type": "task_item",
                "bundle_item_id": "bundle_task_" + sha256_text(task.task_id)[:24],
                "task_id": task.task_id,
                "source_candidate_id": task.source_candidate_id,
                "workspace_id": task.workspace_id,
                "project_id": task.project_id,
                "conversation_id": task.conversation_id,
                "topic_segment_id": task.topic_segment_id,
                "summary": task.summary,
                "task_kind": task.task_kind,
                "task_state": task.task_state,
                "evidence_summary": _evidence_summary(task.evidence),
                "suggested_action": task.suggested_action,
                "suggestion_only": True,
                "promotion_eligible": False,
            }
        )
    for noise in consolidation.noise_items:
        items.append(
            {
                "item_type": "noise_item",
                "bundle_item_id": "bundle_noise_" + sha256_text(noise.noise_id)[:24],
                "noise_id": noise.noise_id,
                "source_candidate_id": noise.source_candidate_id,
                "workspace_id": noise.workspace_id,
                "project_id": noise.project_id,
                "conversation_id": noise.conversation_id,
                "topic_segment_id": noise.topic_segment_id,
                "summary": noise.summary,
                "reason": noise.reason,
                "reason_detail": noise.reason_detail,
                "source_message_id": noise.source_message_id,
                "suggested_action": noise.suggested_action,
                "suggestion_only": True,
                "promotion_eligible": False,
                "full_text_included": False,
            }
        )
    for question in consolidation.open_questions:
        items.append(
            {
                "item_type": "open_question",
                "bundle_item_id": "bundle_oq_" + sha256_text(question.open_question_id)[:24],
                "open_question_id": question.open_question_id,
                "candidate_id": question.candidate_id,
                "workspace_id": question.workspace_id,
                "project_id": question.project_id,
                "conversation_id": question.conversation_id,
                "topic_segment_id": question.topic_segment_id,
                "question": question.question,
                "evidence_summary": _evidence_summary(question.evidence),
                "suggested_action": "keep_open",
                "suggestion_only": True,
                "status": question.status,
                "resolved": question.resolved,
                "promotion_eligible": False,
            }
        )
    order = {"parent_group": 0, "grouped_candidate": 1, "task_item": 2, "noise_item": 3, "open_question": 4}
    items.sort(key=lambda item: (order[item["item_type"]], item["bundle_item_id"]))
    return items


def _review_stats(
    extraction: ExtractionResult,
    consolidation: ConsolidationResult,
    bundle: list[dict[str, Any]],
    observations: tuple[Any, ...] = (),
    state_resolution: Any | None = None,
    observation_generation_issues: int = 0,
) -> dict[str, Any]:
    parent_groups = [item for item in bundle if item["item_type"] == "parent_group"]
    grouped = [item for item in bundle if item["item_type"] == "grouped_candidate"]
    tasks = [item for item in bundle if item["item_type"] == "task_item"]
    noise = [item for item in bundle if item["item_type"] == "noise_item"]
    questions = [item for item in bundle if item["item_type"] == "open_question"]
    stats = consolidation.stats()
    nested_group_ids = {group_id for parent in consolidation.parent_groups for group_id in parent.member_group_ids}
    top_level_parent_count = stats["top_level_parent_group_count"]
    nested_parent_count = stats["nested_parent_group_count"]
    top_level_grouped_count = stats["top_level_grouped_candidate_count"]
    return {
        "raw_candidate_count": stats["raw_candidate_count"],
        "consolidated_group_count": stats["consolidated_candidate_count"],
        "consolidated_candidate_count": stats["consolidated_candidate_count"],
        "top_level_grouped_candidate_count": top_level_grouped_count,
        "nested_group_count": len(nested_group_ids),
        "parent_group_count": stats["parent_group_count"],
        "top_level_parent_group_count": top_level_parent_count,
        "nested_parent_group_count": nested_parent_count,
        "top_level_review_group_count": top_level_grouped_count + top_level_parent_count,
        "nested_child_group_count": len(nested_group_ids),
        "child_claim_count": sum(len(item.child_claims) for item in consolidation.grouped_candidates),
        "grouped_member_count": stats["grouped_member_count"],
        "merged_group_count": stats["merged_group_count"],
        "experimental_group_count": stats["experimental_group_count"],
        "task_item_count": stats["task_item_count"],
        "noise_item_count": stats["noise_item_count"],
        "open_question_count": stats["open_question_count"],
        "suppressed_item_count": stats["suppressed_audit_count"],
        "suppressed_reason_counts": stats["suppressed_reason_counts"],
        "observations": len(observations),
        "state_transitions": len(state_resolution.transitions) if state_resolution is not None else 0,
        "pending_state_reviews": len(state_resolution.pending_review_items) if state_resolution is not None else 0,
        "observation_generation_issues": observation_generation_issues,
        "grouped_candidate_count": len(grouped),
        "candidate_count": len(grouped),
        "suggested_action_counts": dict(sorted(Counter(item["suggested_action"] for item in bundle).items())),
        "candidate_type_counts": dict(sorted(Counter(item["candidate_type"] for item in grouped).items())),
        "risk_counts": dict(sorted(Counter(risk for item in grouped for risk in item["risks"]).items())),
        "candidate_method_counts": dict(sorted(Counter(item.get("extraction_method", "mixed") for item in grouped).items())),
        "manual_review_groups": sum(item["review_status"] == "pending_review" for item in grouped),
        "manual_review_parent_groups": sum(item["review_status"] == "pending_review" for item in parent_groups),
        "task_state_counts": dict(sorted(Counter(item["task_state"] for item in tasks).items())),
        "noise_reason_counts": dict(sorted(Counter(item["reason"] for item in noise).items())),
        "heuristic_questionish_skipped": extraction.questionish_skipped,
        "heuristic_duplicates_suppressed": extraction.heuristic_deduplicated,
        "heuristic_limit_suppressed": extraction.heuristic_limit_suppressed,
        "heuristic_long_sentences_suppressed": extraction.heuristic_long_sentence_suppressed,
        "suggestion_only": True,
        "decisions_recorded": 0,
        "open_questions_auto_closed": 0,
    }


def _review_markdown(bundle: list[dict[str, Any]], stats: dict[str, Any]) -> str:
    parent_groups = [item for item in bundle if item["item_type"] == "parent_group"]
    grouped = [item for item in bundle if item["item_type"] == "grouped_candidate"]
    tasks = [item for item in bundle if item["item_type"] == "task_item"]
    noise = [item for item in bundle if item["item_type"] == "noise_item"]
    questions = [item for item in bundle if item["item_type"] == "open_question"]
    lines = [
        "# Conversation Context Cleaner Review Bundle",
        "",
        "> 建议仅供人工审查，不是 review decision；没有 promotion、canonical、Milvus 或 alias 写入。",
        "",
        "| metric | count |",
        "|---|---:|",
        f"| raw candidates | {stats['raw_candidate_count']} |",
        f"| consolidated groups (all hierarchy levels) | {stats['consolidated_group_count']} |",
        f"| top-level grouped candidates | {stats['top_level_grouped_candidate_count']} |",
        f"| nested grouped candidates | {stats['nested_group_count']} |",
        f"| top-level review entries (grouped + parent) | {stats['top_level_review_group_count']} |",
        f"| nested child groups (compatibility alias) | {stats['nested_child_group_count']} |",
        f"| child claims | {stats['child_claim_count']} |",
        f"| merged groups | {stats['merged_group_count']} |",
        f"| parent groups | {stats['parent_group_count']} |",
        f"| top-level parent groups | {stats['top_level_parent_group_count']} |",
        f"| nested parent groups | {stats['nested_parent_group_count']} |",
        f"| task items | {stats['task_item_count']} |",
        f"| noise items | {stats['noise_item_count']} |",
        f"| open questions | {stats['open_question_count']} |",
        f"| suppressed audit items | {stats['suppressed_item_count']} |",
        f"| observations | {stats['observations']} |",
        f"| state transitions | {stats['state_transitions']} |",
        f"| pending state reviews | {stats['pending_state_reviews']} |",
        f"| observation generation issues | {stats['observation_generation_issues']} |",
        "",
        "## Parent groups",
        "",
        "| parent group | type | members | merge reasons | evidence |",
        "|---|---|---|---|---|",
    ]
    for item in parent_groups:
        evidence = "<br>".join(f"{entry['message_id']}: {entry['quote']}" for entry in item["evidence_summary"][:3]).replace("|", "/")
        lines.append(f"| {item['parent_group_id']} | {item['parent_type']} | {len(item['member_group_ids'])} | {'; '.join(item['merge_reasons']).replace('|', '/')} | {evidence} |")
    lines.extend([
        "",
        "## Grouped candidates",
        "",
        "| group | members | type | merge reasons | suggestion | evidence |",
        "|---|---|---|---|---|---|",
    ])
    for item in grouped:
        evidence = "<br>".join(f"{entry['message_id']}: {entry['quote']}" for entry in item["evidence_summary"][:2]).replace("|", "/")
        lines.append(
            f"| {item['group_id']} | {len(item['member_candidate_ids'])} | {item['candidate_type']} | "
            f"{'; '.join(item['merge_reasons']).replace('|', '/')} | {item['suggested_action']} | {evidence} |"
        )
    lines.extend(["", "## Task history", "", "| task | kind | state | summary |", "|---|---|---|---|"])
    for item in tasks:
        lines.append(f"| {item['task_id']} | {item['task_kind']} | {item['task_state']} | {item['summary'].replace('|', '/')} |")
    lines.extend(["", "## Noise / rejected", "", "| item | reason | summary | source |", "|---|---|---|---|"])
    for item in noise:
        lines.append(f"| {item['noise_id']} | {item['reason']} | {item['summary'].replace('|', '/')} | {item['source_message_id']} |")
    lines.extend(["", "## Open questions", "", "| question | status | suggestion |", "|---|---|---|"])
    for item in questions:
        lines.append(f"| {item['question'].replace('|', '/')} | {item['status']} | {item['suggested_action']} |")
    lines.extend(["", "## Suppressed audit", "", "| reason | count |", "|---|---:|"])
    for reason, count in stats["suppressed_reason_counts"].items():
        lines.append(f"| {reason} | {count} |")
    lines.append("")
    return "\n".join(lines)


def _audit_report(
    plan: dict[str, Any],
    extraction: ExtractionResult,
    consolidation: ConsolidationResult,
    observations: tuple[Any, ...] = (),
    state_resolution: Any | None = None,
    observation_generation_issues: int = 0,
) -> dict[str, Any]:
    stats = consolidation.stats()
    nested_group_ids = {group_id for parent in consolidation.parent_groups for group_id in parent.member_group_ids}
    top_level_parent_count = sum(not parent.parent_group_ids for parent in consolidation.parent_groups)
    nested_parent_count = len(consolidation.parent_groups) - top_level_parent_count
    return {
        "contract_version": CONVERSATION_CONTRACT_VERSION,
        "api_version": CONVERSATION_SKILL_API_VERSION,
        "extractor_version": EXTRACTOR_VERSION,
        "run_id": plan["run_id"],
        "plan_id": plan["plan_id"],
        "status": "review_ready",
        "source_sha256": plan["input"]["sha256"],
        "source_size": plan["input"]["size"],
        "traceable_conversation_ids": list(plan["scope"]["conversation_ids"]),
        "boundary": {"type": plan["scope"]["boundary_type"], "evidence": plan["scope"]["boundary_evidence"]},
        "counts": {
            "messages": len(extraction.messages),
            "topic_segments": len(extraction.segments),
            "raw_candidates": stats["raw_candidate_count"],
            "consolidated_candidates": stats["consolidated_candidate_count"],
            "consolidated_groups": stats["consolidated_candidate_count"],
            "top_level_grouped_candidates": stats["consolidated_candidate_count"] - len(nested_group_ids),
            "nested_groups": len(nested_group_ids),
            "top_level_review_groups": (stats["consolidated_candidate_count"] - len(nested_group_ids)) + top_level_parent_count,
            "grouped_members": stats["grouped_member_count"],
            "merged_groups": stats["merged_group_count"],
            "experimental_groups": stats["experimental_group_count"],
            "parent_groups": stats["parent_group_count"],
            "top_level_parent_groups": top_level_parent_count,
            "nested_parent_groups": nested_parent_count,
            "nested_child_groups": len(nested_group_ids),
            "task_items": stats["task_item_count"],
            "noise_items": stats["noise_item_count"],
            "open_questions": stats["open_question_count"],
            "suppressed_items": stats["suppressed_audit_count"],
            "suppressed_reason_counts": stats["suppressed_reason_counts"],
            "observations": len(observations),
            "state_transitions": len(state_resolution.transitions) if state_resolution is not None else 0,
            "pending_state_reviews": len(state_resolution.pending_review_items) if state_resolution is not None else 0,
            "observation_generation_issues": observation_generation_issues,
            "evidence_spans_raw": sum(len(item.evidence) for item in extraction.candidates),
            "evidence_spans_grouped": sum(len(item.evidence) for item in consolidation.grouped_candidates),
            "evidence_verified": all(extraction.evidence_checks.values()),
        },
        "topic_merge_audit": [
            {
                "topic_segment_id": segment.topic_segment_id,
                "merged_segment_ids": list(segment.merged_segment_ids),
                "merge_audit": list(segment.merge_audit),
            }
            for segment in extraction.segments
            if segment.merged_segment_ids
        ],
        "consolidation": stats,
        "parent_groups": [
            {
                "parent_group_id": parent.parent_group_id,
                "parent_type": parent.parent_type,
                "member_group_ids": list(parent.member_group_ids),
                "member_candidate_ids": list(parent.member_candidate_ids),
                "merge_reasons": list(parent.merge_reasons),
                "evidence_ids": [item.evidence_id for item in parent.evidence],
            }
            for parent in consolidation.parent_groups
        ],
        "groups": [
            {
                "group_id": group.group_id,
                "claim_key": group.claim_key,
                "member_candidate_ids": list(group.member_candidate_ids),
                "merge_reasons": list(group.merge_reasons),
                "evidence_ids": [item.evidence_id for item in group.evidence],
            }
            for group in consolidation.grouped_candidates
        ],
        "extractor_summary": plan.get("extractor_summary", {}),
        "checks": plan["checks"],
        "warnings": list(extraction.warnings),
        "safety": {
            "auto_scan": False,
            "real_llm_called": False,
            "production_canonical_written": False,
            "milvus_written": False,
            "alias_changed": False,
            "decisions_recorded": 0,
            "promotions_recorded": 0,
            "open_questions_auto_closed": 0,
        },
    }


def _render_artifacts(
    plan: dict[str, Any],
    extraction: ExtractionResult,
    consolidation: ConsolidationResult,
    observations: tuple[Any, ...] = (),
    state_resolution: Any | None = None,
    observation_generation_issues: int = 0,
) -> dict[str, str]:
    bundle = _review_bundle(extraction, consolidation)
    stats = _review_stats(
        extraction,
        consolidation,
        bundle,
        observations=observations,
        state_resolution=state_resolution,
        observation_generation_issues=observation_generation_issues,
    )
    audit = _audit_report(
        plan,
        extraction,
        consolidation,
        observations=observations,
        state_resolution=state_resolution,
        observation_generation_issues=observation_generation_issues,
    )
    rendered = {
        "messages.jsonl": _jsonl([item.to_dict() for item in extraction.messages]),
        "segments.jsonl": _jsonl([item.to_dict() for item in extraction.segments]),
        "raw_candidates.jsonl": _jsonl([item.to_dict() for item in extraction.candidates]),
        "candidates.jsonl": _jsonl([item.to_dict() for item in consolidation.grouped_candidates]),
        "parent_groups.jsonl": _jsonl([item.to_dict() for item in consolidation.parent_groups]),
        "task_history.jsonl": _jsonl([item.to_dict() for item in consolidation.task_items]),
        "noise_items.jsonl": _jsonl([item.to_dict() for item in consolidation.noise_items]),
        "open_questions.jsonl": _jsonl([item.to_dict() for item in consolidation.open_questions]),
        "suppressed_candidates.jsonl": _jsonl(list(consolidation.suppressed_audit)),
        "observations.jsonl": _jsonl([item.to_dict() for item in observations]),
        "state_transitions.jsonl": _jsonl(
            [item.to_dict() for item in state_resolution.transitions] if state_resolution is not None else []
        ),
        "state_review.jsonl": _jsonl(
            [item.to_dict() for item in state_resolution.pending_review_items] if state_resolution is not None else []
        ),
        "review_bundle.jsonl": _jsonl(bundle),
        "review_stats.json": pretty_json(stats) + "\n",
        "REVIEW_BUNDLE.md": _review_markdown(bundle, stats),
        "audit_report.json": pretty_json(audit) + "\n",
    }
    findings = scan_texts_for_secrets(rendered.items())
    if findings:
        raise IncompleteRunError("generated artifacts failed the secret scan")
    return rendered


def _manifest_for(plan: dict[str, Any], rendered: dict[str, str]) -> dict[str, Any]:
    hashes = {name: sha256_text(text) for name, text in sorted(rendered.items())}
    return {
        "contract_version": CONVERSATION_CONTRACT_VERSION,
        "api_version": CONVERSATION_SKILL_API_VERSION,
        "extractor_version": EXTRACTOR_VERSION,
        "plan_id": plan["plan_id"],
        "run_id": plan["run_id"],
        "source_sha256": plan["input"]["sha256"],
        "created_at": utc_now(),
        "artifacts": hashes,
        "artifact_count": len(hashes),
        "review_only": True,
        "promotion_writes": 0,
        "production_writes": False,
    }


def _verify_manifest(directory: Path, *, plan_id: str | None, source_sha256: str | None, artifact_names: tuple[str, ...] | None) -> dict[str, Any]:
    manifest_path = directory / "run_manifest.json"
    if manifest_path.is_symlink():
        raise IncompleteRunError("run manifest is a symlink")
    if not manifest_path.is_file():
        raise IncompleteRunError("run manifest is missing")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise IncompleteRunError("run manifest is unreadable") from exc
    if plan_id is not None and manifest.get("plan_id") != plan_id:
        raise IncompleteRunError("run manifest plan_id does not match")
    if source_sha256 is not None and manifest.get("source_sha256") != source_sha256:
        raise IncompleteRunError("run manifest source hash does not match")
    names = artifact_names if artifact_names is not None else tuple(manifest.get("artifacts", {}).keys())
    if not names:
        raise IncompleteRunError("run manifest contains no artifacts")
    for name in names:
        path = directory / name
        if path.is_symlink():
            raise IncompleteRunError(f"run artifact is a symlink: {name}")
        if not path.is_file():
            raise IncompleteRunError(f"run artifact is missing: {name}")
        expected = manifest.get("artifacts", {}).get(name)
        if expected != sha256_text(path.read_text(encoding="utf-8")):
            raise IncompleteRunError(f"run artifact hash does not match: {name}")
    return manifest


def apply_run(
    plan: dict[str, Any],
    extraction: ExtractionResult,
    consolidation: ConsolidationResult,
    *,
    observations: tuple[Any, ...] = (),
    state_resolution: Any | None = None,
    observation_generation_issues: int = 0,
) -> dict[str, Any]:
    workspace_root = Path(plan["request"]["workspace_root"])
    final_dir = run_directory(workspace_root, plan["run_id"])
    rendered = _render_artifacts(
        plan,
        extraction,
        consolidation,
        observations=observations,
        state_resolution=state_resolution,
        observation_generation_issues=observation_generation_issues,
    )
    manifest = _manifest_for(plan, rendered)
    if final_dir.exists():
        if final_dir.is_symlink():
            raise IncompleteRunError("run directory is a symlink")
        _verify_manifest(final_dir, plan_id=plan["plan_id"], source_sha256=plan["input"]["sha256"], artifact_names=RUN_ARTIFACTS)
        return {
            "status": "already_applied", "changed": False, "plan_id": plan["plan_id"], "run_id": plan["run_id"],
            "run_dir": final_dir, "artifact_hashes": manifest["artifacts"], "counts": plan["counts"],
        }
    runs_dir = final_dir.parent
    runs_dir.mkdir(parents=True, exist_ok=True)
    staging = runs_dir / f".tmp_{uuid.uuid4().hex[:16]}"
    staging.mkdir(parents=False, exist_ok=False)
    try:
        for name, text in rendered.items():
            atomic_write_text(staging / name, text)
        atomic_write_text(staging / "run_manifest.json", pretty_json(manifest) + "\n")
        os.replace(staging, final_dir)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return {
        "status": "applied", "changed": True, "plan_id": plan["plan_id"], "run_id": plan["run_id"],
        "run_dir": final_dir, "artifact_hashes": manifest["artifacts"], "counts": plan["counts"],
    }


def _empty_status(workspace_root: Path) -> dict[str, Any]:
    return {
        "status": "ok", "workspace_root": workspace_root, "state_root": state_root(workspace_root),
        "run_count": 0, "export_count": 0, "runs": [], "exports": [], "warnings": [],
    }


def status(workspace_root: Path) -> dict[str, Any]:
    root = state_root(workspace_root)
    result = _empty_status(workspace_root)
    if not root.exists():
        return result
    runs_root = root / "runs"
    if runs_root.is_symlink():
        result["warnings"].append("runs directory is a symlink; ignored")
    elif runs_root.is_dir():
        for directory in sorted(runs_root.iterdir(), key=lambda item: item.name):
            if directory.is_symlink() or not directory.is_dir() or directory.name.startswith("."):
                continue
            try:
                manifest = _verify_manifest(directory, plan_id=None, source_sha256=None, artifact_names=None)
                result["runs"].append({
                    "run_id": directory.name, "plan_id": manifest.get("plan_id"), "created_at": manifest.get("created_at"),
                    "source_sha256": manifest.get("source_sha256"), "artifact_count": manifest.get("artifact_count", 0), "status": "ok",
                })
            except IncompleteRunError as exc:
                result["warnings"].append(f"{directory.name}: {exc}")
                result["runs"].append({"run_id": directory.name, "status": "incomplete"})
    exports_root = root / "exports"
    if exports_root.is_symlink():
        result["warnings"].append("exports directory is a symlink; ignored")
    elif exports_root.is_dir():
        for directory in sorted(exports_root.iterdir(), key=lambda item: item.name):
            manifest_path = directory / "export_manifest.json"
            if directory.is_symlink() or not directory.is_dir() or manifest_path.is_symlink() or not manifest_path.is_file():
                continue
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                result["exports"].append({
                    "export_id": manifest.get("export_id"), "run_id": manifest.get("run_id"),
                    "created_at": manifest.get("created_at"), "path": directory,
                })
            except (OSError, ValueError):
                result["warnings"].append(f"{directory.name}: export manifest is unreadable")
    result["run_count"] = len(result["runs"])
    result["export_count"] = len(result["exports"])
    if result["warnings"]:
        result["status"] = "warning"
    return result


def review_export(*, workspace_root: Path, run_id: str, output_dir: str | Path | None = None) -> dict[str, Any]:
    source = run_directory(workspace_root, run_id)
    if not source.is_dir() or source.is_symlink():
        raise IncompleteRunError("run does not exist")
    _verify_manifest(source, plan_id=None, source_sha256=None, artifact_names=None)
    available_files = tuple(name for name in EXPORT_FILES if (source / name).is_file())
    required = {"review_bundle.jsonl", "review_stats.json", "REVIEW_BUNDLE.md", "audit_report.json"}
    if not required.issubset(available_files):
        raise IncompleteRunError("run does not contain the required review export artifacts")
    destination = Path(output_dir).resolve(strict=False) if output_dir is not None else state_root(workspace_root) / "exports" / run_id
    ensure_within(destination, workspace_root, label="review export output")
    if destination == source or source in destination.parents:
        raise IncompleteRunError("review export output must not be inside the run directory")
    export_id = "rev_" + sha256_json({"run_id": run_id, "destination": destination.as_posix()})[:20]
    source_manifest_hash = sha256_text((source / "run_manifest.json").read_text(encoding="utf-8"))
    manifest = {
        "contract_version": CONVERSATION_CONTRACT_VERSION,
        "api_version": CONVERSATION_SKILL_API_VERSION,
        "export_id": export_id,
        "run_id": run_id,
        "created_at": utc_now(),
        "source_run_manifest_sha256": source_manifest_hash,
        "artifacts": {name: sha256_text((source / name).read_text(encoding="utf-8")) for name in available_files},
        "suggestion_only": True,
        "decisions_recorded": 0,
        "promotions_recorded": 0,
        "production_writes": False,
    }
    if destination.exists():
        if destination.is_symlink():
            raise IncompleteRunError("review export output is a symlink")
        manifest_path = destination / "export_manifest.json"
        if not manifest_path.is_file():
            raise IncompleteRunError("review export output exists without a manifest")
        try:
            existing = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise IncompleteRunError("existing review export manifest is unreadable") from exc
        if existing.get("export_id") != export_id:
            raise IncompleteRunError("review export output belongs to a different export")
        for name, expected in manifest["artifacts"].items():
            path = destination / name
            if not path.is_file() or sha256_text(path.read_text(encoding="utf-8")) != expected:
                raise IncompleteRunError("existing review export is incomplete")
        return {"status": "already_exported", "changed": False, "export_id": export_id, "output_dir": destination}
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.parent / f".tmp_{uuid.uuid4().hex[:16]}"
    staging.mkdir(parents=False, exist_ok=False)
    try:
        for name in available_files:
            atomic_write_text(staging / name, (source / name).read_text(encoding="utf-8"))
        atomic_write_text(staging / "export_manifest.json", pretty_json(manifest) + "\n")
        os.replace(staging, destination)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return {
        "status": "exported", "changed": True, "export_id": export_id, "run_id": run_id,
        "output_dir": destination, "artifact_hashes": manifest["artifacts"], "suggestion_only": True,
        "decisions_recorded": 0, "promotions_recorded": 0,
    }
