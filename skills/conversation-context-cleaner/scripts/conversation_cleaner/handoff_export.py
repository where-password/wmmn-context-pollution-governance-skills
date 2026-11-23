from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .contracts import ConversationCleanerError
from .handoff_builder import HandoffBuildResult, build_handoff
from .handoff_contracts import HANDOFF_CONTRACT_VERSION, MemoryObservation, StateTransition
from .security import (
    assert_safe_path,
    ensure_within,
    scan_text_for_secrets,
    scan_texts_for_secrets,
    validate_workspace_root,
)
from .state_resolution import resolve_state_transitions
from .storage import _verify_manifest, run_directory, state_root
from .util import atomic_write_text, pretty_json, sha256_json, sha256_text, utc_now


HANDOFF_DATA_ARTIFACTS = (
    "memory_write_batch.jsonl",
    "source_units.jsonl",
    "state_transitions.jsonl",
    "review_pending.jsonl",
    "HANDOFF_REVIEW.md",
)
HANDOFF_MANIFEST = "handoff_manifest.json"
HANDOFF_CONTRACT_NAME = "conversation-memory-handoff"
_RUN_ID_RE = re.compile(r"^ccr_[0-9a-f]{24}$")


class HandoffExportError(ConversationCleanerError):
    code = "handoff_export_error"


class HandoffExportPathError(HandoffExportError):
    code = "handoff_path_error"


class HandoffExportInputError(HandoffExportError):
    code = "handoff_input_error"


class HandoffExportConflictError(HandoffExportError):
    code = "handoff_export_conflict"


def _jsonl(records: list[dict[str, Any]]) -> str:
    return "".join(json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n" for item in records)


def _summary(text: str, limit: int = 220) -> str:
    clean = re.sub(r"\s+", " ", text).strip()
    return clean if len(clean) <= limit else clean[: limit - 3].rstrip() + "..."


def _markdown_cell(text: str) -> str:
    return _summary(text).replace("|", "\\|")


def _time_key(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise HandoffExportInputError("source timestamp must include an offset")
    return parsed.astimezone(timezone.utc)


def _validate_observations_location(observations_root: str | Path, observations_path: str | Path) -> tuple[Path, Path]:
    raw_root = Path(observations_root).expanduser()
    if raw_root.is_symlink():
        raise HandoffExportPathError("observations root must not be a symlink")
    root = assert_safe_path(raw_root, label="observations root")
    if not root.is_dir():
        raise HandoffExportPathError("observations root must be an existing directory")

    raw_file = Path(observations_path).expanduser()
    if raw_file.is_symlink():
        raise HandoffExportPathError("observations file must not be a symlink")
    source = assert_safe_path(raw_file, label="observations file")
    ensure_within(source, root, label="observations file")
    if source.is_symlink() or not source.is_file():
        raise HandoffExportPathError("observations file must be an existing regular file")
    return root, source


def _read_and_validate_observations(path: Path) -> tuple[tuple[MemoryObservation, ...], str, str]:
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HandoffExportInputError("observations file must be UTF-8") from exc
    if scan_text_for_secrets(text):
        raise HandoffExportInputError("observations input failed secret scan")

    observations: list[MemoryObservation] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            raise HandoffExportInputError(f"observations line {line_number} is empty")
        try:
            record = json.loads(line)
        except ValueError as exc:
            raise HandoffExportInputError(f"observations line {line_number} is not valid JSON") from exc
        if not isinstance(record, dict):
            raise HandoffExportInputError(f"observations line {line_number} must be an object")
        try:
            observations.append(MemoryObservation.from_dict(record))
        except ValueError as exc:
            raise HandoffExportInputError(f"observations line {line_number} failed strict contract validation") from exc
    if not observations:
        raise HandoffExportInputError("observations file contains no records")
    return tuple(observations), hashlib.sha256(raw).hexdigest(), text


def _read_run(workspace_root: Path, run_id: str) -> tuple[Path, dict[str, Any], dict[str, dict[str, Any]]]:
    if not _RUN_ID_RE.match(run_id):
        raise HandoffExportInputError("run_id has an invalid format")
    directory = run_directory(workspace_root, run_id)
    if directory.is_symlink() or not directory.is_dir():
        raise HandoffExportInputError("run does not exist")
    manifest = _verify_manifest(directory, plan_id=None, source_sha256=None, artifact_names=None)
    if manifest.get("run_id") != run_id:
        raise HandoffExportInputError("run manifest run_id does not match")
    if manifest.get("review_only") is not True or manifest.get("production_writes") is not False:
        raise HandoffExportInputError("run manifest is not a safe review-only run")
    if not isinstance(manifest.get("source_sha256"), str) or not manifest["source_sha256"]:
        raise HandoffExportInputError("run manifest source SHA is missing")

    messages_path = directory / "messages.jsonl"
    if messages_path.is_symlink() or not messages_path.is_file():
        raise HandoffExportInputError("run messages artifact is missing")
    text = messages_path.read_text(encoding="utf-8")
    by_source_unit: dict[str, dict[str, Any]] = {}
    for line_number, line in enumerate(text.splitlines(), start=1):
        try:
            message = json.loads(line)
        except ValueError as exc:
            raise HandoffExportInputError(f"run messages line {line_number} is invalid") from exc
        source_unit_id = message.get("source_unit_id")
        if not isinstance(source_unit_id, str) or not source_unit_id:
            raise HandoffExportInputError(f"run messages line {line_number} lacks source_unit_id")
        if source_unit_id in by_source_unit:
            raise HandoffExportInputError("run messages contain duplicate source_unit_id values")
        by_source_unit[source_unit_id] = message
    if not by_source_unit:
        raise HandoffExportInputError("run messages artifact contains no records")
    return directory, manifest, by_source_unit


def _validate_source_compatibility(
    observations: tuple[MemoryObservation, ...],
    messages_by_source_unit: dict[str, dict[str, Any]],
) -> None:
    for observation in observations:
        evidence_sequences: set[int] = set()
        timestamp_matches = False
        for evidence in observation.evidences:
            message = messages_by_source_unit.get(evidence.source_unit_id)
            if message is None:
                raise HandoffExportInputError("observation evidence source_unit_id is not present in the run")
            if message.get("conversation_id") != observation.conversation_id:
                raise HandoffExportInputError("observation evidence belongs to another conversation")
            text = message.get("text")
            if not isinstance(text, str):
                raise HandoffExportInputError("run message text is missing")
            locator = evidence.locator_json
            message_id = locator.get("message_id")
            if message_id is not None and message_id != message.get("message_id"):
                raise HandoffExportInputError("evidence locator message_id does not match the run")
            start = locator.get("char_start")
            end = locator.get("char_end")
            if start is not None or end is not None:
                if (
                    isinstance(start, bool)
                    or isinstance(end, bool)
                    or not isinstance(start, int)
                    or not isinstance(end, int)
                    or start < 0
                    or end < start
                    or end > len(text)
                    or text[start:end] != evidence.quote
                ):
                    raise HandoffExportInputError("evidence character span does not match the run message")
            elif evidence.quote not in text:
                raise HandoffExportInputError("evidence quote is not present in the run message")
            sequence = message.get("sequence")
            if isinstance(sequence, int) and not isinstance(sequence, bool):
                evidence_sequences.add(sequence)
            timestamp = message.get("timestamp")
            if not isinstance(timestamp, str):
                raise HandoffExportInputError("run message timestamp is missing")
            if _time_key(timestamp) == _time_key(observation.observed_at):
                timestamp_matches = True
        if observation.source_sequence not in evidence_sequences or not timestamp_matches:
            raise HandoffExportInputError("observation time/sequence is not compatible with its evidence messages")


def _handoff_identity(
    *,
    run_id: str,
    run_source_sha256: str,
    observations_sha256: str,
    workspace_id: str,
    project_id: str,
    domain_profile: str,
    profile_version: str,
    source_id: str,
) -> str:
    material = {
        "handoff_contract_version": HANDOFF_CONTRACT_VERSION,
        "run_id": run_id,
        "run_source_sha256": run_source_sha256,
        "observations_sha256": observations_sha256,
        "workspace_id": workspace_id,
        "project_id": project_id,
        "domain_profile": domain_profile,
        "profile_version": profile_version,
        "source_id": source_id,
    }
    return "handoff_" + sha256_json(material)[:24]


def _render_source_units(
    *,
    messages_by_source_unit: dict[str, dict[str, Any]],
    write_items: tuple[Any, ...],
    source_id: str,
    workspace_id: str,
    project_id: str,
    source_sha256: str,
) -> list[dict[str, Any]]:
    attributes_by_unit: dict[str, set[tuple[str, str]]] = {}
    for item in write_items:
        for evidence in item.evidences:
            unit_id = evidence.source_unit_id
            message = messages_by_source_unit.get(unit_id)
            if message is None:
                raise HandoffExportInputError("source unit referenced by evidence is missing from messages")
            text_content = message.get("text")
            if not isinstance(text_content, str) or not text_content:
                raise HandoffExportInputError("source unit message text is missing")
            if evidence.quote not in text_content:
                raise HandoffExportInputError("evidence quote is not a substring of source unit text")
            message_hash = message.get("source_unit_hash")
            if not isinstance(message_hash, str) or not message_hash:
                raise HandoffExportInputError("source unit hash is missing from messages")
            locator_hash = evidence.locator_json.get("source_unit_hash")
            if locator_hash is not None and locator_hash != message_hash:
                raise HandoffExportInputError("evidence source unit hash does not match messages")
            if message.get("source_unit_id") != unit_id:
                raise HandoffExportInputError("source unit id does not match messages")
            attributes_by_unit.setdefault(unit_id, set()).add(
                (item.requested_visibility, item.declared_sensitivity)
            )

    if not attributes_by_unit:
        raise HandoffExportInputError("handoff contains no evidence source units")

    if not isinstance(source_sha256, str) or not source_sha256:
        raise HandoffExportInputError("run source_sha256 is missing")
    records: list[dict[str, Any]] = []
    for unit_id in sorted(attributes_by_unit):
        attributes = attributes_by_unit[unit_id]
        if len(attributes) != 1:
            raise HandoffExportInputError("source unit has conflicting visibility/sensitivity attributes")
        visibility, sensitivity = next(iter(attributes))
        message = messages_by_source_unit[unit_id]
        message_id = message.get("message_id")
        conversation_id = message.get("conversation_id")
        role = message.get("role")
        sequence = message.get("sequence")
        timestamp = message.get("timestamp")
        if not all(isinstance(value, str) and value for value in (message_id, conversation_id, role, timestamp)):
            raise HandoffExportInputError("source unit message identity fields are incomplete")
        if isinstance(sequence, bool) or not isinstance(sequence, int):
            raise HandoffExportInputError("source unit message sequence is invalid")
        records.append({
            "source_id": source_id,
            "workspace_id": workspace_id,
            "project_id": project_id,
            "source_unit_id": unit_id,
            "parent_unit_id": None,
            "scope_type": "conversation_message",
            "scope_ref": message_id,
            "source_unit_hash": message["source_unit_hash"],
            "text_content": message["text"],
            "conversation_id": conversation_id,
            "message_id": message_id,
            "role": role,
            "sequence": sequence,
            "timestamp": timestamp,
            "source_type": "conversation_message",
            "source_path": f"conversation:{conversation_id}",
            "source_sha256": source_sha256,
            "visibility": visibility,
            "sensitivity": sensitivity,
            "metadata": {
                "message_id": message_id,
                "conversation_id": conversation_id,
                "role": role,
                "sequence": sequence,
                "timestamp": timestamp,
            },
        })
    return records


def _review_markdown(
    *,
    handoff_id: str,
    run_id: str,
    observations_sha256: str,
    build_result: HandoffBuildResult,
    transitions: tuple[StateTransition, ...],
    source_unit_count: int,
) -> str:
    new_items = [item for item in build_result.write_items if item.transition_kind == "new"]
    supersede_items = [item for item in build_result.write_items if item.transition_kind == "supersedes"]
    lines = [
        "# Conversation Memory Handoff Review",
        "",
        "> Suggestion-only handoff. No canonical database, Milvus, or alias write has occurred.",
        "",
        f"- handoff_id: `{handoff_id}`",
        f"- run_id: `{run_id}`",
        f"- observations_sha256: `{observations_sha256}`",
        f"- handoff_contract: `{HANDOFF_CONTRACT_VERSION}`",
        f"- state transitions: `{len(transitions)}`",
        f"- proposed write items: `{len(build_result.write_items)}`",
        f"- pending review items: `{len(build_result.pending_review_items)}`",
        f"- skipped observations: `{len(build_result.skipped_observations)}`",
        f"- source units: `{source_unit_count}`",
        "",
        "## Current writes",
        "",
        "| idempotency key | claim | content | evidence |",
        "|---|---|---|---:|",
    ]
    for item in new_items:
        lines.append(f"| `{item.idempotency_key}` | `{_markdown_cell(item.claim_key)}` | {_markdown_cell(item.content)} | {len(item.evidences)} |")
    lines.extend([
        "",
        "## Supersedes",
        "",
        "| idempotency key | claim | supersedes_ref | new content | evidence |",
        "|---|---|---|---|---:|",
    ])
    for item in supersede_items:
        lines.append(f"| `{item.idempotency_key}` | `{_markdown_cell(item.claim_key)}` | `{item.supersedes_ref}` | {_markdown_cell(item.content)} | {len(item.evidences)} |")
    lines.extend([
        "",
        "## Pending review",
        "",
        "| review id | observation | transition | reason |",
        "|---|---|---|---|",
    ])
    for item in build_result.pending_review_items:
        lines.append(f"| `{item.review_item_id}` | `{item.observation_id}` | `{item.transition_kind}` | {_markdown_cell(item.reason)} |")
    lines.extend([
        "",
        "## Skipped",
        "",
        "| observation | transition | reason |",
        "|---|---|---|",
    ])
    for item in build_result.skipped_observations:
        lines.append(f"| `{item.observation_id}` | `{item.transition_kind}` | {_markdown_cell(item.reason)} |")
    lines.extend([
        "",
        "## Database boundary",
        "",
        "The database adapter must authorize and revalidate each item, resolve `supersedes_ref` to a concrete `supersedes_id`, enforce version and security policy, and complete the actual transaction. This review file is not proof of persistence.",
        "",
    ])
    return "\n".join(lines)


def _render_handoff(
    *,
    handoff_id: str,
    run_id: str,
    run_manifest: dict[str, Any],
    observations_sha256: str,
    workspace_id: str,
    project_id: str,
    domain_profile: str,
    profile_version: str,
    source_id: str,
    build_result: HandoffBuildResult,
    transitions: tuple[StateTransition, ...],
    conversation_ids: tuple[str, ...],
    source_units: list[dict[str, Any]],
) -> tuple[dict[str, str], dict[str, Any]]:
    rendered = {
        "memory_write_batch.jsonl": _jsonl([item.to_dict() for item in build_result.write_items]),
        "source_units.jsonl": _jsonl(source_units),
        "state_transitions.jsonl": _jsonl([item.to_dict() for item in transitions]),
        "review_pending.jsonl": _jsonl([item.to_dict() for item in build_result.pending_review_items]),
        "HANDOFF_REVIEW.md": _review_markdown(
            handoff_id=handoff_id,
            run_id=run_id,
            observations_sha256=observations_sha256,
            build_result=build_result,
            transitions=transitions,
            source_unit_count=len(source_units),
        ),
    }
    findings = scan_texts_for_secrets(rendered.items())
    if findings:
        raise HandoffExportInputError("generated handoff artifacts failed secret scan")
    artifact_hashes = {name: sha256_text(text) for name, text in sorted(rendered.items())}
    counts = {
        **build_result.counts.to_dict(),
        "state_transitions": len(transitions),
        "source_units": len(source_units),
        "new_writes": sum(item.transition_kind == "new" for item in build_result.write_items),
        "superseding_writes": sum(item.transition_kind == "supersedes" for item in build_result.write_items),
    }
    manifest = {
        "handoff_contract": HANDOFF_CONTRACT_NAME,
        "handoff_contract_version": HANDOFF_CONTRACT_VERSION,
        "handoff_id": handoff_id,
        "created_at": utc_now(),
        "run_id": run_id,
        "plan_id": run_manifest.get("plan_id"),
        "source_sha256": run_manifest.get("source_sha256"),
        "observations_sha256": observations_sha256,
        "conversation_ids": list(conversation_ids),
        "workspace_id": workspace_id,
        "project_id": project_id,
        "domain_profile": domain_profile,
        "profile_version": profile_version,
        "source_id": source_id,
        "counts": counts,
        "artifacts": artifact_hashes,
        "review_only": True,
        "production_writes": False,
        "real_llm_called": False,
        "milvus_written": False,
        "alias_changed": False,
        "canonical_writes": False,
        "contains_security_fields": False,
    }
    return rendered, manifest


def _verify_existing_handoff(
    destination: Path,
    *,
    handoff_id: str,
    run_id: str,
    observations_sha256: str,
    workspace_id: str,
    project_id: str,
    domain_profile: str,
    profile_version: str,
    source_id: str,
) -> dict[str, Any]:
    manifest_path = destination / HANDOFF_MANIFEST
    if destination.is_symlink() or not destination.is_dir() or manifest_path.is_symlink() or not manifest_path.is_file():
        raise HandoffExportConflictError("existing handoff is incomplete")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HandoffExportConflictError("existing handoff manifest is unreadable") from exc
    expected_identity = {
        "handoff_contract": HANDOFF_CONTRACT_NAME,
        "handoff_contract_version": HANDOFF_CONTRACT_VERSION,
        "handoff_id": handoff_id,
        "run_id": run_id,
        "observations_sha256": observations_sha256,
        "workspace_id": workspace_id,
        "project_id": project_id,
        "domain_profile": domain_profile,
        "profile_version": profile_version,
        "source_id": source_id,
    }
    for key, expected in expected_identity.items():
        if manifest.get(key) != expected:
            raise HandoffExportConflictError("existing handoff identity does not match")
    if (
        manifest.get("production_writes") is not False
        or manifest.get("real_llm_called") is not False
        or manifest.get("milvus_written") is not False
        or manifest.get("alias_changed") is not False
    ):
        raise HandoffExportConflictError("existing handoff safety flags are invalid")
    artifact_hashes = manifest.get("artifacts")
    if not isinstance(artifact_hashes, dict) or set(artifact_hashes) != set(HANDOFF_DATA_ARTIFACTS):
        raise HandoffExportConflictError("existing handoff artifact manifest is invalid")
    for name, expected_hash in artifact_hashes.items():
        path = destination / name
        if path.is_symlink() or not path.is_file() or sha256_text(path.read_text(encoding="utf-8")) != expected_hash:
            raise HandoffExportConflictError("existing handoff artifact hash does not match")
    return manifest


def _write_handoff(
    *,
    workspace_root: Path,
    handoff_id: str,
    rendered: dict[str, str],
    manifest: dict[str, Any],
) -> Path:
    handoffs_root = state_root(workspace_root) / "handoffs"
    ensure_within(handoffs_root, workspace_root, label="handoff output")
    if handoffs_root.is_symlink():
        raise HandoffExportPathError("handoffs root must not be a symlink")
    destination = handoffs_root / handoff_id
    if destination.suffix or destination.name != handoff_id:
        raise HandoffExportPathError("invalid handoff destination")
    if destination.is_symlink():
        raise HandoffExportConflictError("handoff destination must not be a symlink")
    handoffs_root.mkdir(parents=True, exist_ok=True)
    staging = handoffs_root / f".tmp_{uuid.uuid4().hex[:16]}"
    staging.mkdir(parents=False, exist_ok=False)
    try:
        for name, text in rendered.items():
            atomic_write_text(staging / name, text)
        atomic_write_text(staging / HANDOFF_MANIFEST, pretty_json(manifest) + "\n")
        os.replace(staging, destination)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return destination


def export_handoff(
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
    """Export isolated database handoff artifacts without database writes."""

    for field_name, value in (
        ("workspace_id", workspace_id),
        ("project_id", project_id),
        ("domain_profile", domain_profile),
        ("profile_version", profile_version),
        ("source_id", source_id),
    ):
        if not isinstance(value, str) or not value.strip():
            raise HandoffExportInputError(f"{field_name} must be a non-empty string")

    root = validate_workspace_root(workspace_root)
    _, observations_file = _validate_observations_location(observations_root, observations_path)
    observations, observations_sha256, _ = _read_and_validate_observations(observations_file)
    _, run_manifest, messages = _read_run(root, run_id)
    _validate_source_compatibility(observations, messages)

    handoff_id = _handoff_identity(
        run_id=run_id,
        run_source_sha256=run_manifest["source_sha256"],
        observations_sha256=observations_sha256,
        workspace_id=workspace_id,
        project_id=project_id,
        domain_profile=domain_profile,
        profile_version=profile_version,
        source_id=source_id,
    )
    destination = state_root(root) / "handoffs" / handoff_id
    ensure_within(destination, root, label="handoff output")

    if destination.is_symlink():
        raise HandoffExportConflictError("handoff destination must not be a symlink")
    if destination.exists():
        existing = _verify_existing_handoff(
            destination,
            handoff_id=handoff_id,
            run_id=run_id,
            observations_sha256=observations_sha256,
            workspace_id=workspace_id,
            project_id=project_id,
            domain_profile=domain_profile,
            profile_version=profile_version,
            source_id=source_id,
        )
        return {
            "status": "already_exported",
            "changed": False,
            "handoff_id": handoff_id,
            "run_id": run_id,
            "output_dir": destination,
            "counts": existing["counts"],
            "artifact_hashes": existing["artifacts"],
            "production_writes": False,
            "real_llm_called": False,
            "milvus_written": False,
            "alias_changed": False,
        }

    try:
        resolved = resolve_state_transitions(observations)
        built = build_handoff(
            resolved.observations,
            resolved.transitions,
            workspace_id=workspace_id,
            project_id=project_id,
            domain_profile=domain_profile,
            profile_version=profile_version,
            source_id=source_id,
        )
    except ValueError as exc:
        raise HandoffExportInputError("resolved observations could not be converted to a handoff") from exc

    conversation_ids = tuple(sorted({observation.conversation_id for observation in observations}))
    if not conversation_ids:
        raise HandoffExportInputError("exported observations must contain at least one conversation_id")
    source_units = _render_source_units(
        messages_by_source_unit=messages,
        write_items=built.write_items,
        source_id=source_id,
        workspace_id=workspace_id,
        project_id=project_id,
        source_sha256=run_manifest.get("source_sha256"),
    )
    rendered, manifest = _render_handoff(
        handoff_id=handoff_id,
        run_id=run_id,
        run_manifest=run_manifest,
        observations_sha256=observations_sha256,
        workspace_id=workspace_id,
        project_id=project_id,
        domain_profile=domain_profile,
        profile_version=profile_version,
        source_id=source_id,
        build_result=built,
        transitions=resolved.transitions,
        conversation_ids=conversation_ids,
        source_units=source_units,
    )
    output_dir = _write_handoff(
        workspace_root=root,
        handoff_id=handoff_id,
        rendered=rendered,
        manifest=manifest,
    )
    return {
        "status": "exported",
        "changed": True,
        "handoff_id": handoff_id,
        "run_id": run_id,
        "output_dir": output_dir,
        "counts": manifest["counts"],
        "artifact_hashes": manifest["artifacts"],
        "production_writes": False,
        "real_llm_called": False,
        "milvus_written": False,
        "alias_changed": False,
    }
