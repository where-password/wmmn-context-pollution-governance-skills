from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from .api_v1 import (
    clean_apply,
    clean_plan,
    doctor,
    handoff_export,
    handoff_review_auto_approve,
    handoff_review_decide,
    handoff_review_export_approved,
    handoff_review_status,
    review_export,
    status,
)
from .handoff_approval import APPROVAL_DECISIONS
from .contracts import ALLOWED_BOUNDARY_TYPES
from .security import assert_safe_path
from .util import jsonable, pretty_json


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


EXIT_OK = 0
EXIT_BLOCKED = 2
EXIT_ERROR = 1
_OK_STATUSES = {"ok", "warning", "planned", "applied", "already_applied", "exported", "already_exported", "recorded", "duplicate", "generated", "already_generated", "recorded", "no_safe_items"}


def _json_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="emit compact stable JSON")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="context-cleaner",
        description="Conversation Context Cleaner V1 (review-only, deterministic, stdlib-only)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    doctor_parser = sub.add_parser("doctor", help="check local runtime and paths without writing")
    doctor_parser.add_argument("--workspace-root", required=True)
    doctor_parser.add_argument("--input-root")
    doctor_parser.add_argument("--input")
    _json_flag(doctor_parser)

    clean = sub.add_parser("clean", help="plan or explicitly apply a review-only clean run")
    clean_sub = clean.add_subparsers(dest="clean_command", required=True)

    plan = clean_sub.add_parser("plan", help="build and print an exact plan without writing")
    plan.add_argument("--input-root")
    plan.add_argument("--input")
    plan.add_argument("--workspace-root")
    plan.add_argument("--workspace-id")
    plan.add_argument("--project-id")
    plan.add_argument("--source-id")
    plan.add_argument("--domain-profile")
    plan.add_argument("--profile-version")
    plan.add_argument("--conversation-id", action="append")
    plan.add_argument("--boundary-type", choices=ALLOWED_BOUNDARY_TYPES)
    plan.add_argument("--boundary-evidence")
    plan.add_argument("--topic-gap-seconds", type=int, default=1800)
    plan.add_argument("--max-messages", type=int, default=10_000)
    plan.add_argument("--heuristic-per-message-cap", type=int, default=30)
    plan.add_argument("--heuristic-total-cap", type=int, default=100)
    plan.add_argument("--preset")
    _json_flag(plan)

    apply_parser = clean_sub.add_parser("apply", help="apply an exact saved plan after explicit confirmation")
    apply_parser.add_argument("--plan", required=True)
    apply_parser.add_argument("--confirm", required=True)
    _json_flag(apply_parser)

    status_parser = sub.add_parser("status", help="read isolated run and export status without writing")
    status_parser.add_argument("--workspace-root", required=True)
    _json_flag(status_parser)

    export_parser = sub.add_parser("review", help="review package operations")
    export_sub = export_parser.add_subparsers(dest="review_command", required=True)
    export = export_sub.add_parser("export", help="export a review package from an applied run")
    export.add_argument("--workspace-root", required=True)
    export.add_argument("--run-id", required=True)
    export.add_argument("--output-dir")
    _json_flag(export)

    handoff_parser = sub.add_parser("handoff", help="database handoff operations")
    handoff_sub = handoff_parser.add_subparsers(dest="handoff_command", required=True)
    handoff_export_parser = handoff_sub.add_parser("export", help="export isolated database handoff artifacts")
    handoff_export_parser.add_argument("--workspace-root")
    handoff_export_parser.add_argument("--run-id")
    handoff_export_parser.add_argument("--observations-root")
    handoff_export_parser.add_argument("--observations")
    handoff_export_parser.add_argument("--workspace-id")
    handoff_export_parser.add_argument("--project-id")
    handoff_export_parser.add_argument("--domain-profile")
    handoff_export_parser.add_argument("--profile-version")
    handoff_export_parser.add_argument("--source-id")
    handoff_export_parser.add_argument("--preset")
    _json_flag(handoff_export_parser)

    handoff_review_parser = handoff_sub.add_parser("review", help="persisted handoff approval review")
    handoff_review_sub = handoff_review_parser.add_subparsers(dest="handoff_review_command", required=True)

    decide_parser = handoff_review_sub.add_parser("decide", help="append an explicit handoff decision")
    decide_parser.add_argument("--workspace-root", required=True)
    decide_parser.add_argument("--handoff-id", required=True)
    decide_parser.add_argument("--idempotency-key", required=True)
    decide_parser.add_argument("--decision", required=True, choices=APPROVAL_DECISIONS)
    decide_parser.add_argument("--reviewer-id", required=True)
    decide_parser.add_argument("--reason", required=True)
    decide_parser.add_argument("--reviewed-at", required=True)
    _json_flag(decide_parser)

    auto_approve_parser = handoff_review_sub.add_parser(
        "auto-approve",
        help="approve only quote-verified user decisions with no supersede/conflict/risk markers",
    )
    auto_approve_parser.add_argument("--workspace-root", required=True)
    auto_approve_parser.add_argument("--handoff-id", required=True)
    auto_approve_parser.add_argument("--reviewer-id", default="local-auto-approval")
    auto_approve_parser.add_argument(
        "--reason",
        default="auto-approved: quote-verified user decision without supersede/conflict",
    )
    auto_approve_parser.add_argument("--reviewed-at")
    _json_flag(auto_approve_parser)

    review_status_parser = handoff_review_sub.add_parser("status", help="read current approval decisions")
    review_status_parser.add_argument("--workspace-root", required=True)
    review_status_parser.add_argument("--handoff-id", required=True)
    _json_flag(review_status_parser)

    export_approved_parser = handoff_review_sub.add_parser("export-approved", help="generate current approved batch")
    export_approved_parser.add_argument("--workspace-root", required=True)
    export_approved_parser.add_argument("--handoff-id", required=True)
    _json_flag(export_approved_parser)
    return parser


def _emit(payload: dict[str, Any], *, json_mode: bool) -> None:
    print(json.dumps(jsonable(payload), ensure_ascii=False, sort_keys=True) if json_mode else pretty_json(payload))


def _exit_code(payload: dict[str, Any]) -> int:
    return EXIT_OK if str(payload.get("status")) in _OK_STATUSES else EXIT_BLOCKED


def _load_preset(path_value: str) -> dict[str, Any]:
    path = assert_safe_path(path_value, label="preset file")
    if not path.is_file() or path.is_symlink():
        raise ValueError("preset file must be an existing regular file")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("preset file must be valid UTF-8 JSON") from exc
    if not isinstance(value, dict) or value.get("contract_version") != "conversation-cleaner-preset-v1":
        raise ValueError("unsupported conversation cleaner preset")
    allowed = {
        "workspace_root", "input_root", "input", "workspace_id", "project_id", "source_id",
        "domain_profile", "profile_version", "conversation_id", "boundary_type",
        "boundary_evidence", "observations_root", "observations", "reviewer_id", "reason",
    }
    unknown = set(value) - allowed - {"contract_version"}
    if unknown:
        raise ValueError("preset contains unknown fields")
    result: dict[str, Any] = {}
    for name, item in value.items():
        if name == "contract_version":
            continue
        if name == "conversation_id":
            if isinstance(item, str):
                result[name] = [item]
            elif isinstance(item, list) and all(isinstance(entry, str) and entry.strip() for entry in item):
                result[name] = item
            else:
                raise ValueError("preset conversation_id must be a string or string array")
        elif name in {"workspace_root", "input_root", "input", "observations", "observations_root"}:
            if not isinstance(item, str) or not item.strip():
                raise ValueError(f"preset {name} must be a path string")
            candidate = Path(item).expanduser()
            result[name] = str(candidate if candidate.is_absolute() else (path.parent / candidate).resolve())
        else:
            if not isinstance(item, str) or not item.strip():
                raise ValueError(f"preset {name} must be a non-empty string")
            result[name] = item
    return result


def _apply_preset(args: argparse.Namespace) -> None:
    if not getattr(args, "preset", None):
        return
    for name, value in _load_preset(str(args.preset)).items():
        if hasattr(args, name) and getattr(args, name) in (None, "", []):
            setattr(args, name, value)


def _require_fields(args: argparse.Namespace, names: tuple[str, ...]) -> None:
    missing = [name for name in names if getattr(args, name, None) in (None, "", [])]
    if missing:
        raise ValueError("missing required fields: " + ", ".join(missing))


def _load_plan(path_value: str) -> dict[str, Any]:
    path = assert_safe_path(path_value, label="plan file")
    if not path.is_file():
        raise ValueError("plan file does not exist")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("plan file must contain a JSON object")
    return value


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    json_mode = bool(getattr(args, "json", False))
    try:
        _apply_preset(args)
        if args.command == "clean" and args.clean_command == "plan":
            _require_fields(args, (
                "input_root", "input", "workspace_root", "workspace_id", "project_id",
                "source_id", "domain_profile", "profile_version", "boundary_type", "boundary_evidence",
            ))
            if not args.conversation_id:
                raise ValueError("missing required fields: conversation_id")
        elif args.command == "handoff" and args.handoff_command == "export":
            _require_fields(args, (
                "workspace_root", "run_id", "observations_root", "observations", "workspace_id",
                "project_id", "domain_profile", "profile_version", "source_id",
            ))
        if args.command == "doctor":
            payload = doctor(
                workspace_root=args.workspace_root,
                input_root=args.input_root,
                input_path=args.input,
            )
        elif args.command == "clean" and args.clean_command == "plan":
            payload = clean_plan(
                input_root=args.input_root,
                input_path=args.input,
                workspace_root=args.workspace_root,
                workspace_id=args.workspace_id,
                project_id=args.project_id,
                source_id=args.source_id,
                domain_profile=args.domain_profile,
                profile_version=args.profile_version,
                conversation_ids=tuple(args.conversation_id),
                boundary_type=args.boundary_type,
                boundary_evidence=args.boundary_evidence,
                topic_gap_seconds=args.topic_gap_seconds,
                max_messages=args.max_messages,
                heuristic_per_message_cap=args.heuristic_per_message_cap,
                heuristic_total_cap=args.heuristic_total_cap,
            )
        elif args.command == "clean" and args.clean_command == "apply":
            payload = clean_apply(_load_plan(args.plan), confirmation_token=args.confirm)
        elif args.command == "handoff" and args.handoff_command == "export":
            payload = handoff_export(
                workspace_root=args.workspace_root,
                run_id=args.run_id,
                observations_root=args.observations_root,
                observations_path=args.observations,
                workspace_id=args.workspace_id,
                project_id=args.project_id,
                domain_profile=args.domain_profile,
                profile_version=args.profile_version,
                source_id=args.source_id,
            )
        elif args.command == "handoff" and args.handoff_command == "review" and args.handoff_review_command == "decide":
            payload = handoff_review_decide(
                workspace_root=args.workspace_root,
                handoff_id=args.handoff_id,
                idempotency_key=args.idempotency_key,
                decision=args.decision,
                reviewer_id=args.reviewer_id,
                reason=args.reason,
                reviewed_at=args.reviewed_at,
            )
        elif args.command == "handoff" and args.handoff_command == "review" and args.handoff_review_command == "auto-approve":
            payload = handoff_review_auto_approve(
                workspace_root=args.workspace_root,
                handoff_id=args.handoff_id,
                reviewer_id=args.reviewer_id,
                reason=args.reason,
                reviewed_at=args.reviewed_at,
            )
        elif args.command == "handoff" and args.handoff_command == "review" and args.handoff_review_command == "status":
            payload = handoff_review_status(
                workspace_root=args.workspace_root,
                handoff_id=args.handoff_id,
            )
        elif args.command == "handoff" and args.handoff_command == "review" and args.handoff_review_command == "export-approved":
            payload = handoff_review_export_approved(
                workspace_root=args.workspace_root,
                handoff_id=args.handoff_id,
            )
        elif args.command == "status":
            payload = status(workspace_root=args.workspace_root)
        elif args.command == "review" and args.review_command == "export":
            payload = review_export(
                workspace_root=args.workspace_root,
                run_id=args.run_id,
                output_dir=args.output_dir,
            )
        else:
            raise ValueError("unsupported command")
    except Exception as exc:
        payload = {
            "status": "error",
            "error": {"code": "runtime_error", "message": f"{type(exc).__name__}: {exc}"},
        }
    _emit(payload, json_mode=json_mode)
    return _exit_code(payload)
