from __future__ import annotations

import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Mapping

from .handoff_export import export_handoff as _export_handoff
from .handoff_review import (
    HandoffReviewError,
    generate_approved_batch,
    handoff_approval_status,
    read_handoff_items,
    record_handoff_decision,
)
from .handoff_quick import auto_approve_handoff
from .contracts import (
    ALLOWED_BOUNDARY_TYPES,
    CONVERSATION_CONTRACT_VERSION,
    CONVERSATION_SKILL_API_VERSION,
    CONTROLLED_INPUT_CONTRACT,
    EXTRACTOR_VERSION,
    ConversationCleanerError,
    PlanBlockedError,
    PlanBuild,
    Scope,
)
from .consolidation import consolidate_candidates
from .extraction import extract_candidates
from .parsing import parse_controlled_jsonl
from .security import (
    ensure_within,
    scan_text_for_secrets,
    validate_input_location,
    validate_workspace_root,
)
from .observation_generation import ObservationGenerationError, generate_observations
from .segmentation import segment_messages
from .state_resolution import StateResolutionError, resolve_state_transitions
from .storage import apply_run, review_export as _review_export, state_root, status as _status
from .util import sha256_json, sha256_text, utc_now


_RUN_ID_RE = re.compile(r"^ccr_[0-9a-f]{24}$")
_HANDOFF_ID_RE = re.compile(r"^handoff_[0-9a-f]{24}$")


def _normalize_path(value: str | Path) -> str:
    return Path(value).expanduser().resolve(strict=False).as_posix()


def _normalize_scope(scope: Mapping[str, Any]) -> dict[str, Any]:
    raw_ids = scope.get("conversation_ids", [])
    conversation_ids = [item for item in raw_ids if isinstance(item, str) and item.strip()]
    return {
        "workspace_id": str(scope.get("workspace_id", "")).strip(),
        "project_id": str(scope.get("project_id", "")).strip(),
        "source_id": str(scope.get("source_id", "")).strip(),
        "domain_profile": str(scope.get("domain_profile", "")).strip(),
        "profile_version": str(scope.get("profile_version", "")).strip(),
        "conversation_ids": conversation_ids,
        "boundary_type": str(scope.get("boundary_type", "")).strip(),
        "boundary_evidence": str(scope.get("boundary_evidence", "")).strip(),
    }


def _make_request(
    *,
    input_root: str | Path,
    input_path: str | Path,
    workspace_root: str | Path,
    workspace_id: str,
    project_id: str,
    source_id: str,
    domain_profile: str,
    profile_version: str,
    conversation_ids: tuple[str, ...] | list[str],
    boundary_type: str,
    boundary_evidence: str,
    topic_gap_seconds: int,
    max_messages: int,
    heuristic_per_message_cap: int = 30,
    heuristic_total_cap: int = 100,
) -> dict[str, Any]:
    return {
        "input_root": _normalize_path(input_root),
        "input_path": _normalize_path(input_path),
        "workspace_root": _normalize_path(workspace_root),
        "scope": _normalize_scope(
            {
                "workspace_id": workspace_id,
                "project_id": project_id,
                "source_id": source_id,
                "domain_profile": domain_profile,
                "profile_version": profile_version,
                "conversation_ids": list(conversation_ids),
                "boundary_type": boundary_type,
                "boundary_evidence": boundary_evidence,
            }
        ),
        "topic_gap_seconds": topic_gap_seconds,
        "max_messages": max_messages,
        "heuristic_per_message_cap": heuristic_per_message_cap,
        "heuristic_total_cap": heuristic_total_cap,
    }


def _identity(
    request: dict[str, Any],
    *,
    source_sha256: str | None = None,
    extraction_digest: str | None = None,
) -> tuple[str, str, str, Path, Path]:
    material = {
        "contract_version": CONVERSATION_CONTRACT_VERSION,
        "api_version": CONVERSATION_SKILL_API_VERSION,
        "extractor_version": EXTRACTOR_VERSION,
        "request": request,
        "source_sha256": source_sha256,
        "extraction_digest": extraction_digest,
    }
    plan_id = "ccp_" + sha256_json(material)[:24]
    run_id = "ccr_" + sha256_json({"plan_id": plan_id})[:24]
    workspace_root = Path(request["workspace_root"])
    run_dir = state_root(workspace_root) / "runs" / run_id
    confirmation_token = sha256_text(f"apply|{plan_id}|{run_dir.as_posix()}")[:24]
    return plan_id, run_id, confirmation_token, workspace_root, run_dir


def _scope_errors(scope: dict[str, Any]) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    for field in ("workspace_id", "project_id", "source_id", "domain_profile", "profile_version"):
        if not scope.get(field):
            errors.append({"code": "scope_invalid", "message": f"{field} must be explicit and non-empty"})
    if not scope.get("conversation_ids"):
        errors.append({"code": "scope_invalid", "message": "at least one explicit conversation_id is required"})
    if len(scope.get("conversation_ids", [])) != len(set(scope.get("conversation_ids", []))):
        errors.append({"code": "scope_invalid", "message": "conversation_ids must not contain duplicates"})
    if scope.get("boundary_type") not in ALLOWED_BOUNDARY_TYPES:
        errors.append(
            {
                "code": "boundary_invalid",
                "message": "boundary_type must be conversation_closed, task_terminal, or explicit_scan_boundary",
            }
        )
    if not scope.get("boundary_evidence"):
        errors.append({"code": "boundary_invalid", "message": "boundary_evidence must be explicit"})
    return errors


def _blocked_plan(
    request: dict[str, Any],
    errors: list[dict[str, Any]],
    *,
    input_meta: dict[str, Any] | None = None,
    checks: dict[str, bool] | None = None,
    warnings: list[str] | None = None,
    counts: dict[str, int] | None = None,
) -> PlanBuild:
    plan_id, run_id, confirmation_token, workspace_root, run_dir = _identity(request, source_sha256=(input_meta or {}).get("sha256"))
    return PlanBuild(
        plan={
            "contract_version": CONVERSATION_CONTRACT_VERSION,
            "api_version": CONVERSATION_SKILL_API_VERSION,
            "extractor_version": EXTRACTOR_VERSION,
            "input_contract": CONTROLLED_INPUT_CONTRACT,
            "command": "clean plan",
            "status": "blocked",
            "plan_id": plan_id,
            "run_id": run_id,
            "confirmation_token": confirmation_token,
            "created_at": utc_now(),
            "request": request,
            "scope": request["scope"],
            "input": input_meta or {},
            "counts": {
                "messages": 0,
                "topic_segments": 0,
                "raw_candidates": 0,
                "candidates": 0,
                "consolidated_candidates": 0,
                "parent_groups": 0,
                "task_items": 0,
                "noise_items": 0,
                "suppressed_items": 0,
                "open_questions": 0,
                "observations": 0,
                "state_transitions": 0,
                "pending_state_reviews": 0,
                "observation_generation_issues": 0,
                **(counts or {}),
            },
            "checks": checks or {},
            "warnings": warnings or [],
            "errors": errors,
            "output": {
                "relative_path": (Path("conversation-cleaner") / "v1" / "runs" / run_id).as_posix(),
                "absolute_path": run_dir.as_posix(),
                "workspace_root": workspace_root.as_posix(),
            },
        },
        parsed=None,
        extraction=None,
    )


def _generate_observation_stage(extraction: Any, scope: Scope):
    generation = generate_observations(
        extraction.candidates,
        extraction.messages,
        workspace_id=scope.workspace_id,
        project_id=scope.project_id,
        source_id=scope.source_id,
        domain_profile=scope.domain_profile,
        profile_version=scope.profile_version,
    )
    if generation.issues:
        return generation, None
    try:
        resolution = resolve_state_transitions(generation.observations)
    except StateResolutionError as exc:
        raise PlanBlockedError("state resolution failed safely") from exc
    return generation, resolution


def _build_plan(request: dict[str, Any]) -> PlanBuild:
    normalized_request = {
        "input_root": _normalize_path(request.get("input_root", "")),
        "input_path": _normalize_path(request.get("input_path", "")),
        "workspace_root": _normalize_path(request.get("workspace_root", "")),
        "scope": _normalize_scope(request.get("scope", {})),
        "topic_gap_seconds": request.get("topic_gap_seconds", 1800),
        "max_messages": request.get("max_messages", 10_000),
        "heuristic_per_message_cap": request.get("heuristic_per_message_cap", 30),
        "heuristic_total_cap": request.get("heuristic_total_cap", 100),
    }
    scope_data = normalized_request["scope"]
    errors = _scope_errors(scope_data)
    if not isinstance(normalized_request["topic_gap_seconds"], int) or normalized_request["topic_gap_seconds"] <= 0:
        errors.append({"code": "config_invalid", "message": "topic_gap_seconds must be a positive integer"})
    if not isinstance(normalized_request["max_messages"], int) or normalized_request["max_messages"] <= 0:
        errors.append({"code": "config_invalid", "message": "max_messages must be a positive integer"})
    if (
        not isinstance(normalized_request["heuristic_per_message_cap"], int)
        or not 1 <= normalized_request["heuristic_per_message_cap"] <= 100
    ):
        errors.append({"code": "config_invalid", "message": "heuristic_per_message_cap must be an integer between 1 and 100"})
    if (
        not isinstance(normalized_request["heuristic_total_cap"], int)
        or not 1 <= normalized_request["heuristic_total_cap"] <= 500
    ):
        errors.append({"code": "config_invalid", "message": "heuristic_total_cap must be an integer between 1 and 500"})
    try:
        workspace_root = validate_workspace_root(normalized_request["workspace_root"])
    except ConversationCleanerError as exc:
        errors.append({"code": exc.code, "message": str(exc)})
        return _blocked_plan(normalized_request, errors)
    if errors:
        return _blocked_plan(normalized_request, errors, checks={"scope_explicit": False})

    scope = Scope(
        workspace_id=scope_data["workspace_id"],
        project_id=scope_data["project_id"],
        source_id=scope_data["source_id"],
        domain_profile=scope_data["domain_profile"],
        profile_version=scope_data["profile_version"],
        conversation_ids=tuple(scope_data["conversation_ids"]),
        boundary_type=scope_data["boundary_type"],
        boundary_evidence=scope_data["boundary_evidence"],
    )
    try:
        parsed = parse_controlled_jsonl(
            input_root=normalized_request["input_root"],
            input_path=normalized_request["input_path"],
            source_id=scope.source_id,
            allowed_conversation_ids=scope.conversation_ids,
            max_messages=normalized_request["max_messages"],
        )
    except ConversationCleanerError as exc:
        errors.append({"code": exc.code, "message": str(exc)})
        return _blocked_plan(normalized_request, errors)

    input_meta = {
        "path": normalized_request["input_path"],
        "root": normalized_request["input_root"],
        "sha256": parsed.source_sha256,
        "size": parsed.source_size,
        "line_count": parsed.total_lines,
    }
    secret_findings = scan_text_for_secrets(parsed.raw_text)
    for finding in secret_findings:
        errors.append(
            {
                "code": "secret_scan_blocked",
                "message": "sensitive marker categories detected; matched values were not retained",
                "line": finding["line"],
                "categories": finding["categories"],
            }
        )
    errors.extend(parsed.errors)
    if errors:
        return _blocked_plan(
            normalized_request,
            errors,
            input_meta=input_meta,
            checks={"schema_valid": not parsed.errors, "secret_scan_passed": not secret_findings},
            warnings=parsed.warnings,
        )

    segments = segment_messages(parsed.messages, topic_gap_seconds=normalized_request["topic_gap_seconds"])
    extraction = extract_candidates(
        messages=parsed.messages,
        segments=segments,
        scope=scope,
        per_message_cap=normalized_request["heuristic_per_message_cap"],
        total_cap=normalized_request["heuristic_total_cap"],
    )
    consolidation = consolidate_candidates(
        candidates=extraction.candidates,
        open_questions=extraction.open_questions,
        extraction_suppressed_audit=extraction.suppressed_audit,
        messages=parsed.messages,
    )
    consolidation_stats = consolidation.stats()
    evidence_ok = all(extraction.evidence_checks.values())
    checks = {
        "input_explicit_file": True,
        "input_within_explicit_root": True,
        "required_fields_valid": True,
        "timestamps_have_offset": True,
        "source_sha256_recorded": True,
        "workspace_project_explicit": True,
        "boundary_explicit": True,
        "message_sequence_contiguous": not any(
            item.get("code") in {"sequence_gap", "sequence_duplicate"} for item in parsed.errors
        ),
        "topic_segments_built": bool(extraction.segments),
        "deterministic_candidates_extracted": True,
        "evidence_spans_verified": evidence_ok,
        "secret_scan_passed": not secret_findings,
        "manual_input_only": True,
        "no_recursive_discovery": True,
        "no_real_llm": True,
        "no_production_dependencies": True,
        "heuristic_user_messages_only": True,
        "heuristic_question_guard": True,
        "heuristic_conservative_limits": True,
        "task_routing_applied": True,
        "noise_filtering_applied": True,
        "merge_rules_deterministic": True,
        "suppressed_audit_complete": True,
    }
    if not evidence_ok:
        errors.append({"code": "evidence_validation_failed", "message": "one or more evidence spans failed verification"})
        return _blocked_plan(
            normalized_request,
            errors,
            input_meta=input_meta,
            checks=checks,
            warnings=list(extraction.warnings),
        )

    base_counts = {
        "messages": len(extraction.messages),
        "topic_segments": len(extraction.segments),
        "raw_candidates": len(extraction.candidates),
        "candidates": len(extraction.candidates),
        "consolidated_group_count": consolidation_stats["consolidated_group_count"],
        "consolidated_candidates": len(consolidation.grouped_candidates),
        "top_level_grouped_candidate_count": consolidation_stats["top_level_grouped_candidate_count"],
        "nested_group_count": consolidation_stats["nested_group_count"],
        "top_level_review_group_count": consolidation_stats["top_level_review_group_count"],
        "parent_groups": len(consolidation.parent_groups),
        "task_items": len(consolidation.task_items),
        "noise_items": len(consolidation.noise_items),
        "suppressed_items": len(consolidation.suppressed_audit),
        "open_questions": len(extraction.open_questions),
        "observations": 0,
        "state_transitions": 0,
        "pending_state_reviews": 0,
        "observation_generation_issues": 0,
    }
    try:
        observation_generation, state_resolution = _generate_observation_stage(extraction, scope)
    except (ObservationGenerationError, PlanBlockedError):
        return _blocked_plan(
            normalized_request,
            [{"code": "observation_generation_blocked", "message": "observation generation failed safely; source text was not retained"}],
            input_meta=input_meta,
            checks={**checks, "observation_generation_valid": False, "state_resolution_valid": False},
            warnings=list(extraction.warnings),
            counts={**base_counts, "observation_generation_issues": 1},
        )
    if observation_generation.issues:
        issue_errors = [
            {
                "code": item.issue_code,
                "message": item.detail,
                "issue_id": item.issue_id,
                "candidate_ids": list(item.candidate_ids),
                "source_sequence": item.source_sequence,
            }
            for item in observation_generation.issues
        ]
        return _blocked_plan(
            normalized_request,
            issue_errors,
            input_meta=input_meta,
            checks={**checks, "observation_generation_valid": False, "state_resolution_valid": False},
            warnings=list(extraction.warnings),
            counts={
                **base_counts,
                "observations": len(observation_generation.observations),
                "observation_generation_issues": len(observation_generation.issues),
            },
        )
    assert state_resolution is not None
    checks["observation_generation_valid"] = True
    checks["state_resolution_valid"] = True

    extraction_digest = sha256_json(
        {
            "messages": [
                {"source_unit_id": item.source_unit_id, "source_unit_hash": item.source_unit_hash}
                for item in extraction.messages
            ],
            "segments": [
                {"topic_segment_id": item.topic_segment_id, "source_unit_ids": list(item.source_unit_ids)}
                for item in extraction.segments
            ],
            "candidates": [
                {"candidate_id": item.candidate_id, "content_hash": item.content_hash}
                for item in extraction.candidates
            ],
            "open_questions": [
                {"open_question_id": item.open_question_id, "content_hash": item.content_hash}
                for item in extraction.open_questions
            ],
            "consolidation": {
                "groups": [
                    {
                        "group_id": item.group_id,
                        "member_candidate_ids": list(item.member_candidate_ids),
                        "claim_key": item.claim_key,
                        "evidence_ids": [evidence.evidence_id for evidence in item.evidence],
                    }
                    for item in consolidation.grouped_candidates
                ],
                "parent_groups": [
                    {
                        "parent_group_id": item.parent_group_id,
                        "member_group_ids": list(item.member_group_ids),
                        "member_candidate_ids": list(item.member_candidate_ids),
                        "claim_key": item.claim_key,
                    }
                    for item in consolidation.parent_groups
                ],
                "task_items": [
                    {"task_id": item.task_id, "source_candidate_id": item.source_candidate_id}
                    for item in consolidation.task_items
                ],
                "noise_items": [
                    {"noise_id": item.noise_id, "source_candidate_id": item.source_candidate_id}
                    for item in consolidation.noise_items
                ],
                "suppressed_ids": [item["suppressed_id"] for item in consolidation.suppressed_audit],
            },
            "observations": [
                {
                    "observation_id": item.observation_id,
                    "claim_key": item.claim_key,
                    "scope_key": item.scope_key,
                    "source_sequence": item.source_sequence,
                    "state_kind": item.state_kind,
                    "evidence_ids": [evidence.evidence_id for evidence in item.evidences],
                }
                for item in observation_generation.observations
            ],
            "state_resolution": {
                "transitions": [
                    {
                        "transition_id": item.transition_id,
                        "transition_kind": item.transition_kind,
                        "from_observation_id": item.from_observation_id,
                        "to_observation_id": item.to_observation_id,
                        "evidence_ids": [evidence.evidence_id for evidence in item.evidences],
                    }
                    for item in state_resolution.transitions
                ],
                "pending_state_reviews": [item.review_item_id for item in state_resolution.pending_review_items],
            },
        }
    )
    extractor_summary = {
        "explicit_candidate_count": extraction.explicit_candidate_count,
        "heuristic_candidate_count": extraction.heuristic_candidate_count,
        "questionish_skipped": extraction.questionish_skipped,
        "heuristic_deduplicated": extraction.heuristic_deduplicated,
        "heuristic_limit_suppressed": extraction.heuristic_limit_suppressed,
        "heuristic_long_sentence_suppressed": extraction.heuristic_long_sentence_suppressed,
        "heuristic_rule_counts": dict(
            sorted(
                Counter(
                    item.heuristic_rule
                    for item in extraction.candidates
                    if item.heuristic_rule is not None
                ).items()
            )
        ),
        "consolidation": consolidation_stats,
        "observation_generation": {
            "observations": len(observation_generation.observations),
            "issues": len(observation_generation.issues),
        },
        "state_resolution": {
            "transitions": len(state_resolution.transitions),
            "pending_state_reviews": len(state_resolution.pending_review_items),
            "current_observations": len(state_resolution.current_observation_ids),
        },
        "max_heuristic_per_message": extraction.heuristic_per_message_cap,
        "max_heuristic_total": extraction.heuristic_total_cap,
        "max_heuristic_quote_chars": 360,
    }
    consolidation_summary = {
        "groups": [
            {
                "group_id": item.group_id,
                "member_candidate_ids": list(item.member_candidate_ids),
                "merge_reasons": list(item.merge_reasons),
                "overlap_with": list(item.overlap_with),
                "evidence_count": len(item.evidence),
            }
            for item in consolidation.grouped_candidates
        ],
        "parent_groups": [
            {
                "parent_group_id": item.parent_group_id,
                "parent_type": item.parent_type,
                "member_group_ids": list(item.member_group_ids),
                "member_candidate_ids": list(item.member_candidate_ids),
            }
            for item in consolidation.parent_groups
        ],
        "task_items": [
            {"task_id": item.task_id, "source_candidate_id": item.source_candidate_id, "task_kind": item.task_kind}
            for item in consolidation.task_items
        ],
        "noise_items": [
            {"noise_id": item.noise_id, "source_candidate_id": item.source_candidate_id, "reason": item.reason}
            for item in consolidation.noise_items
        ],
        "suppressed_item_count": len(consolidation.suppressed_audit),
        "suppressed_reason_counts": consolidation_stats["suppressed_reason_counts"],
    }
    plan_id, run_id, confirmation_token, workspace_root, run_dir = _identity(normalized_request, source_sha256=parsed.source_sha256, extraction_digest=extraction_digest)
    plan = {
        "contract_version": CONVERSATION_CONTRACT_VERSION,
        "api_version": CONVERSATION_SKILL_API_VERSION,
        "extractor_version": EXTRACTOR_VERSION,
        "input_contract": CONTROLLED_INPUT_CONTRACT,
        "command": "clean plan",
        "status": "planned",
        "plan_id": plan_id,
        "run_id": run_id,
        "confirmation_token": confirmation_token,
        "created_at": utc_now(),
        "request": normalized_request,
        "scope": normalized_request["scope"],
        "input": input_meta,
        "extraction_digest": extraction_digest,
        "extractor_summary": extractor_summary,
        "consolidation_summary": consolidation_summary,
        "counts": {
            "messages": len(extraction.messages),
            "topic_segments": len(extraction.segments),
            "raw_candidates": len(extraction.candidates),
            "candidates": len(extraction.candidates),
            "consolidated_group_count": consolidation_stats["consolidated_group_count"],
            "consolidated_candidates": len(consolidation.grouped_candidates),
            "top_level_grouped_candidate_count": consolidation_stats["top_level_grouped_candidate_count"],
            "nested_group_count": consolidation_stats["nested_group_count"],
            "top_level_review_group_count": consolidation_stats["top_level_review_group_count"],
            "parent_groups": len(consolidation.parent_groups),
            "task_items": len(consolidation.task_items),
            "noise_items": len(consolidation.noise_items),
            "suppressed_items": len(consolidation.suppressed_audit),
            "open_questions": len(extraction.open_questions),
            "observations": len(observation_generation.observations),
            "state_transitions": len(state_resolution.transitions),
            "pending_state_reviews": len(state_resolution.pending_review_items),
            "observation_generation_issues": len(observation_generation.issues),
        },
        "checks": checks,
        "warnings": list(extraction.warnings),
        "errors": [],
        "output": {
            "relative_path": (Path("conversation-cleaner") / "v1" / "runs" / run_id).as_posix(),
            "absolute_path": run_dir.as_posix(),
            "workspace_root": workspace_root.as_posix(),
        },
    }
    return PlanBuild(plan=plan, parsed=parsed, extraction=extraction, consolidation=consolidation)


def doctor(
    *,
    workspace_root: str | Path,
    input_root: str | Path | None = None,
    input_path: str | Path | None = None,
) -> dict[str, Any]:
    checks: dict[str, bool] = {
        "python_supported": sys.version_info >= (3, 11),
        "stdlib_only": True,
        "manual_input_only": True,
        "no_real_llm": True,
    }
    warnings: list[str] = []
    errors: list[dict[str, Any]] = []
    try:
        root = validate_workspace_root(workspace_root)
        checks["workspace_path_safe"] = True
        checks["isolated_state_root"] = state_root(root).name == "v1"
    except ConversationCleanerError as exc:
        return {
            "contract_version": CONVERSATION_CONTRACT_VERSION,
            "api_version": CONVERSATION_SKILL_API_VERSION,
            "command": "doctor",
            "status": "blocked",
            "created_at": utc_now(),
            "python": sys.version.split()[0],
            "checks": checks,
            "external_dependencies": [],
            "warnings": warnings,
            "errors": [{"code": exc.code, "message": str(exc)}],
        }
    if (input_root is None) != (input_path is None):
        errors.append({"code": "input_pair_required", "message": "input_root and input_path must be provided together"})
    elif input_root is not None and input_path is not None:
        try:
            _, source = validate_input_location(input_root, input_path)
            checks["input_path_safe"] = True
            checks["input_file_exists"] = source.is_file()
            raw = source.read_bytes()
            checks["input_size_within_limit"] = len(raw) <= 10 * 1024 * 1024
            try:
                text = raw.decode("utf-8-sig")
                findings = scan_text_for_secrets(text)
                checks["input_secret_scan_passed"] = not findings
                if findings:
                    warnings.append("input contains sensitive marker categories; clean plan will block")
            except UnicodeDecodeError:
                checks["input_utf8"] = False
                errors.append({"code": "encoding_invalid", "message": "input must be UTF-8"})
        except (ConversationCleanerError, OSError) as exc:
            errors.append({"code": getattr(exc, "code", "input_error"), "message": str(exc)})
    status_value = "blocked" if errors else ("warning" if warnings else "ok")
    return {
        "contract_version": CONVERSATION_CONTRACT_VERSION,
        "api_version": CONVERSATION_SKILL_API_VERSION,
        "command": "doctor",
        "status": status_value,
        "created_at": utc_now(),
        "python": sys.version.split()[0],
        "workspace_root": root,
        "state_root": state_root(root),
        "checks": checks,
        "external_dependencies": [],
        "warnings": warnings,
        "errors": errors,
    }


def clean_plan(
    *,
    input_root: str | Path,
    input_path: str | Path,
    workspace_root: str | Path,
    workspace_id: str,
    project_id: str,
    source_id: str,
    domain_profile: str,
    profile_version: str,
    conversation_ids: tuple[str, ...] | list[str],
    boundary_type: str,
    boundary_evidence: str,
    topic_gap_seconds: int = 1800,
    max_messages: int = 10_000,
    heuristic_per_message_cap: int = 30,
    heuristic_total_cap: int = 100,
) -> dict[str, Any]:
    request = _make_request(
        input_root=input_root,
        input_path=input_path,
        workspace_root=workspace_root,
        workspace_id=workspace_id,
        project_id=project_id,
        source_id=source_id,
        domain_profile=domain_profile,
        profile_version=profile_version,
        conversation_ids=conversation_ids,
        boundary_type=boundary_type,
        boundary_evidence=boundary_evidence,
        topic_gap_seconds=topic_gap_seconds,
        max_messages=max_messages,
        heuristic_per_message_cap=heuristic_per_message_cap,
        heuristic_total_cap=heuristic_total_cap,
    )
    return _build_plan(request).plan


def clean_apply(plan: Mapping[str, Any], *, confirmation_token: str) -> dict[str, Any]:
    try:
        if plan.get("contract_version") != CONVERSATION_CONTRACT_VERSION:
            raise PlanBlockedError("unsupported plan contract version")
        if plan.get("api_version") != CONVERSATION_SKILL_API_VERSION:
            raise PlanBlockedError("unsupported plan API version")
        if plan.get("status") != "planned":
            raise PlanBlockedError("only planned clean plans can be applied")
        request = plan.get("request")
        if not isinstance(request, Mapping):
            raise PlanBlockedError("plan request is missing or invalid")
        rebuilt = _build_plan(dict(request))
        if rebuilt.plan["status"] != "planned":
            raise PlanBlockedError("plan no longer reproduces a clean result")
        if (
            rebuilt.plan["plan_id"] != plan.get("plan_id")
            or rebuilt.plan["confirmation_token"] != plan.get("confirmation_token")
        ):
            raise PlanBlockedError("plan is stale or has been modified")
        if confirmation_token != rebuilt.plan["confirmation_token"]:
            raise PlanBlockedError("confirmation token does not match the exact plan")
        assert rebuilt.extraction is not None
        assert rebuilt.consolidation is not None
        observation_generation, state_resolution = _generate_observation_stage(
            rebuilt.extraction,
            Scope(**rebuilt.plan["request"]["scope"]),
        )
        if observation_generation.issues or state_resolution is None:
            raise PlanBlockedError("observation generation produced review issues")
        applied = apply_run(
            rebuilt.plan,
            rebuilt.extraction,
            rebuilt.consolidation,
            observations=observation_generation.observations,
            state_resolution=state_resolution,
            observation_generation_issues=len(observation_generation.issues),
        )
        return {
            "contract_version": CONVERSATION_CONTRACT_VERSION,
            "api_version": CONVERSATION_SKILL_API_VERSION,
            "command": "clean apply",
            "created_at": utc_now(),
            **applied,
        }
    except ConversationCleanerError as exc:
        return {
            "contract_version": CONVERSATION_CONTRACT_VERSION,
            "api_version": CONVERSATION_SKILL_API_VERSION,
            "command": "clean apply",
            "status": "blocked",
            "changed": False,
            "created_at": utc_now(),
            "error": {"code": exc.code, "message": str(exc)},
        }


def status(*, workspace_root: str | Path) -> dict[str, Any]:
    try:
        root = validate_workspace_root(workspace_root)
    except ConversationCleanerError as exc:
        return {
            "contract_version": CONVERSATION_CONTRACT_VERSION,
            "api_version": CONVERSATION_SKILL_API_VERSION,
            "command": "status",
            "status": "blocked",
            "created_at": utc_now(),
            "error": {"code": exc.code, "message": str(exc)},
        }
    result = _status(root)
    return {
        "contract_version": CONVERSATION_CONTRACT_VERSION,
        "api_version": CONVERSATION_SKILL_API_VERSION,
        "command": "status",
        "created_at": utc_now(),
        **result,
        "production_writes": False,
    }


def review_export(
    *,
    workspace_root: str | Path,
    run_id: str,
    output_dir: str | Path | None = None,
) -> dict[str, Any]:
    try:
        if not _RUN_ID_RE.match(run_id):
            raise PlanBlockedError("run_id has an invalid format")
        root = validate_workspace_root(workspace_root)
        destination: Path | None = None
        if output_dir is not None:
            destination = Path(output_dir).expanduser().resolve(strict=False)
            ensure_within(destination, root, label="review export output")
        result = _review_export(workspace_root=root, run_id=run_id, output_dir=destination)
        return {
            "contract_version": CONVERSATION_CONTRACT_VERSION,
            "api_version": CONVERSATION_SKILL_API_VERSION,
            "command": "review export",
            "created_at": utc_now(),
            **result,
        }
    except ConversationCleanerError as exc:
        return {
            "contract_version": CONVERSATION_CONTRACT_VERSION,
            "api_version": CONVERSATION_SKILL_API_VERSION,
            "command": "review export",
            "status": "blocked",
            "changed": False,
            "created_at": utc_now(),
            "error": {"code": exc.code, "message": str(exc)},
        }


def handoff_export(
    *,
    workspace_root: str | Path,
    run_id: str,
    observations_root: str | Path,
    observations_path: str | Path,
    workspace_id: str,
    project_id: str,
    domain_profile: str,
    profile_version: str,
    source_id: str,
) -> dict[str, Any]:
    """Export a deterministic database handoff without writing production state."""

    try:
        result = _export_handoff(
            workspace_root=workspace_root,
            run_id=run_id,
            observations_root=observations_root,
            observations_path=observations_path,
            workspace_id=workspace_id,
            project_id=project_id,
            domain_profile=domain_profile,
            profile_version=profile_version,
            source_id=source_id,
        )
        return {
            "contract_version": CONVERSATION_CONTRACT_VERSION,
            "api_version": CONVERSATION_SKILL_API_VERSION,
            "handoff_contract_version": "conversation-memory-handoff-v1",
            "command": "handoff export",
            "created_at": utc_now(),
            **result,
        }
    except ConversationCleanerError as exc:
        return {
            "contract_version": CONVERSATION_CONTRACT_VERSION,
            "api_version": CONVERSATION_SKILL_API_VERSION,
            "handoff_contract_version": "conversation-memory-handoff-v1",
            "command": "handoff export",
            "status": "blocked",
            "changed": False,
            "created_at": utc_now(),
            "error": {"code": exc.code, "message": str(exc)},
        }


def _resolve_handoff_directory(workspace_root: str | Path, handoff_id: str) -> Path:
    root = validate_workspace_root(workspace_root)
    if not isinstance(handoff_id, str) or not _HANDOFF_ID_RE.fullmatch(handoff_id):
        raise PlanBlockedError("handoff_id has an invalid format")
    handoffs_root = state_root(root) / "handoffs"
    if handoffs_root.is_symlink():
        raise PlanBlockedError("handoffs root must not be a symlink")
    directory = handoffs_root / handoff_id
    if directory.is_symlink():
        raise PlanBlockedError("handoff directory must not be a symlink")
    ensure_within(directory, handoffs_root, label="handoff directory")
    if not directory.is_dir():
        raise PlanBlockedError("handoff does not exist")
    return directory


def _handoff_review_blocked(command: str, exc: Exception) -> dict[str, Any]:
    return {
        "contract_version": CONVERSATION_CONTRACT_VERSION,
        "api_version": CONVERSATION_SKILL_API_VERSION,
        "command": command,
        "status": "blocked",
        "changed": False,
        "created_at": utc_now(),
        "error": {"code": "handoff_review_error", "message": str(exc)},
    }


def handoff_review_decide(
    *,
    workspace_root: str | Path,
    handoff_id: str,
    idempotency_key: str,
    decision: str,
    reviewer_id: str,
    reason: str,
    reviewed_at: str,
) -> dict[str, Any]:
    """Persist one explicit handoff review decision."""

    try:
        directory = _resolve_handoff_directory(workspace_root, handoff_id)
        items = read_handoff_items(directory)
        item = items.get(idempotency_key)
        if item is None:
            raise HandoffReviewError("handoff item idempotency_key is unknown")
        result = record_handoff_decision(
            handoff_dir=directory,
            handoff_id=handoff_id,
            item=item,
            decision=decision,
            reviewer_id=reviewer_id,
            reason=reason,
            reviewed_at=reviewed_at,
        )
        return {
            "contract_version": CONVERSATION_CONTRACT_VERSION,
            "api_version": CONVERSATION_SKILL_API_VERSION,
            "command": "handoff review decide",
            "created_at": utc_now(),
            **result,
        }
    except (ConversationCleanerError, HandoffReviewError) as exc:
        return _handoff_review_blocked("handoff review decide", exc)


def handoff_review_auto_approve(
    *,
    workspace_root: str | Path,
    handoff_id: str,
    reviewer_id: str = "local-auto-approval",
    reason: str = "auto-approved: quote-verified user decision without supersede/conflict",
    reviewed_at: str | None = None,
) -> dict[str, Any]:
    """Approve only explicitly safe handoff items; leave the rest for human review."""

    try:
        directory = _resolve_handoff_directory(workspace_root, handoff_id)
        result = auto_approve_handoff(
            handoff_dir=directory,
            handoff_id=handoff_id,
            reviewer_id=reviewer_id,
            reason=reason,
            reviewed_at=reviewed_at,
        )
        return {
            "contract_version": CONVERSATION_CONTRACT_VERSION,
            "api_version": CONVERSATION_SKILL_API_VERSION,
            "command": "handoff review auto-approve",
            "created_at": utc_now(),
            **result,
        }
    except (ConversationCleanerError, HandoffReviewError) as exc:
        return _handoff_review_blocked("handoff review auto-approve", exc)


def handoff_review_status(*, workspace_root: str | Path, handoff_id: str) -> dict[str, Any]:
    """Read handoff approval status without writing any artifact."""

    try:
        directory = _resolve_handoff_directory(workspace_root, handoff_id)
        result = handoff_approval_status(handoff_dir=directory, handoff_id=handoff_id)
        return {
            "contract_version": CONVERSATION_CONTRACT_VERSION,
            "api_version": CONVERSATION_SKILL_API_VERSION,
            "command": "handoff review status",
            "status": "ok",
            "changed": False,
            "created_at": utc_now(),
            **result,
        }
    except (ConversationCleanerError, HandoffReviewError) as exc:
        return _handoff_review_blocked("handoff review status", exc)


def handoff_review_export_approved(*, workspace_root: str | Path, handoff_id: str) -> dict[str, Any]:
    """Generate the current approved batch from persisted decisions."""

    try:
        directory = _resolve_handoff_directory(workspace_root, handoff_id)
        result = generate_approved_batch(handoff_dir=directory, handoff_id=handoff_id)
        return {
            "contract_version": CONVERSATION_CONTRACT_VERSION,
            "api_version": CONVERSATION_SKILL_API_VERSION,
            "command": "handoff review export-approved",
            "created_at": utc_now(),
            **result,
        }
    except (ConversationCleanerError, HandoffReviewError) as exc:
        return _handoff_review_blocked("handoff review export-approved", exc)


ConversationSkillApi = {
    "doctor": doctor,
    "clean_plan": clean_plan,
    "clean_apply": clean_apply,
    "status": status,
    "review_export": review_export,
    "handoff_export": handoff_export,
    "handoff_review_decide": handoff_review_decide,
    "handoff_review_auto_approve": handoff_review_auto_approve,
    "handoff_review_status": handoff_review_status,
    "handoff_review_export_approved": handoff_review_export_approved,
}
