from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .contracts import Message, ParsedInput
from .security import validate_input_location
from .util import sha256_bytes, sha256_json


MAX_INPUT_BYTES = 10 * 1024 * 1024
MAX_MESSAGE_CHARS = 200_000
_MAX_ERRORS = 50
_ATTACHMENT_FIELDS = {
    "attachment_id",
    "name",
    "media_type",
    "size",
    "sha256",
    "locator",
    "extraction_status",
}
_REQUIRED_FIELDS = {
    "conversation_id",
    "message_id",
    "role",
    "timestamp",
    "sequence",
    "text",
    "attachments",
}
_OPTIONAL_FIELDS = {"event_type", "source_revision", "metadata"}
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _error(errors: list[dict[str, Any]], code: str, message: str, *, line: int | None = None) -> None:
    if len(errors) >= _MAX_ERRORS:
        return
    item: dict[str, Any] = {"code": code, "message": message}
    if line is not None:
        item["line"] = line
    errors.append(item)


def _parse_timestamp(value: Any) -> tuple[str, str] | None:
    if not isinstance(value, str) or not value.strip():
        return None
    candidate = value.strip()
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    normalized = parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return value, normalized


def _normalize_attachments(value: Any) -> tuple[tuple[dict[str, Any], ...] | None, str | None]:
    if not isinstance(value, list):
        return None, "attachments must be an array"
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            return None, f"attachment[{index}] must be an object"
        unknown = set(item) - _ATTACHMENT_FIELDS
        missing = _ATTACHMENT_FIELDS - set(item)
        if unknown:
            return None, f"attachment[{index}] has unsupported fields"
        if missing:
            return None, f"attachment[{index}] is missing required fields"
        if not isinstance(item["attachment_id"], str) or not item["attachment_id"]:
            return None, f"attachment[{index}].attachment_id is invalid"
        if not isinstance(item["name"], str) or not item["name"]:
            return None, f"attachment[{index}].name is invalid"
        if not isinstance(item["media_type"], str) or not item["media_type"]:
            return None, f"attachment[{index}].media_type is invalid"
        if isinstance(item["size"], bool) or not isinstance(item["size"], int) or item["size"] < 0:
            return None, f"attachment[{index}].size is invalid"
        if not isinstance(item["sha256"], str) or not _SHA256_RE.match(item["sha256"].casefold()):
            return None, f"attachment[{index}].sha256 is invalid"
        if not isinstance(item["locator"], str) or not item["locator"]:
            return None, f"attachment[{index}].locator is invalid"
        if item["extraction_status"] not in {"extracted", "unsupported", "not_found"}:
            return None, f"attachment[{index}].extraction_status is invalid"
        normalized.append(
            {
                "attachment_id": item["attachment_id"],
                "name": item["name"],
                "media_type": item["media_type"],
                "size": item["size"],
                "sha256": item["sha256"].casefold(),
                "locator": item["locator"],
                "extraction_status": item["extraction_status"],
            }
        )
    return tuple(normalized), None


def parse_controlled_jsonl(
    *,
    input_root: str | Path,
    input_path: str | Path,
    source_id: str,
    allowed_conversation_ids: tuple[str, ...],
    max_messages: int = 10_000,
) -> ParsedInput:
    _, source = validate_input_location(input_root, input_path)
    raw_bytes = source.read_bytes()
    if len(raw_bytes) > MAX_INPUT_BYTES:
        return ParsedInput(
            raw_text="",
            source_sha256=sha256_bytes(raw_bytes),
            source_size=len(raw_bytes),
            errors=[{"code": "input_too_large", "message": "input exceeds the 10 MiB limit"}],
        )
    try:
        raw_text = raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        return ParsedInput(
            raw_text="",
            source_sha256=sha256_bytes(raw_bytes),
            source_size=len(raw_bytes),
            errors=[{"code": "encoding_invalid", "message": "input must be UTF-8"}],
        )

    parsed = ParsedInput(
        raw_text=raw_text,
        source_sha256=sha256_bytes(raw_bytes),
        source_size=len(raw_bytes),
        total_lines=len(raw_text.splitlines()),
    )
    allowed = set(allowed_conversation_ids)
    for line_number, line in enumerate(raw_text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            _error(parsed.errors, "json_invalid", "line is not valid JSON", line=line_number)
            continue
        if not isinstance(value, dict):
            _error(parsed.errors, "schema_invalid", "line must contain a JSON object", line=line_number)
            continue
        missing = _REQUIRED_FIELDS - set(value)
        unknown = set(value) - _REQUIRED_FIELDS - _OPTIONAL_FIELDS
        if missing:
            _error(parsed.errors, "schema_invalid", "required fields are missing", line=line_number)
            continue
        if unknown:
            _error(parsed.errors, "schema_invalid", "unsupported top-level fields are present", line=line_number)
            continue
        conversation_id = value["conversation_id"]
        message_id = value["message_id"]
        role = value["role"]
        sequence = value["sequence"]
        text = value["text"]
        event_type = value.get("event_type", "message")
        source_revision = value.get("source_revision")
        metadata = value.get("metadata", {})
        if not isinstance(conversation_id, str) or not conversation_id:
            _error(parsed.errors, "schema_invalid", "conversation_id must be a non-empty string", line=line_number)
            continue
        if conversation_id not in allowed:
            _error(parsed.errors, "conversation_not_allowed", "conversation_id is not in the explicit allowlist", line=line_number)
            continue
        if not isinstance(message_id, str) or not message_id:
            _error(parsed.errors, "schema_invalid", "message_id must be a non-empty string", line=line_number)
            continue
        if role not in {"user", "assistant"}:
            _error(parsed.errors, "schema_invalid", "role must be user or assistant", line=line_number)
            continue
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
            _error(parsed.errors, "schema_invalid", "sequence must be a non-negative integer", line=line_number)
            continue
        if not isinstance(text, str) or not text.strip():
            _error(parsed.errors, "schema_invalid", "text must be a non-empty string", line=line_number)
            continue
        if len(text) > MAX_MESSAGE_CHARS:
            _error(parsed.errors, "message_too_large", "message text exceeds the per-message limit", line=line_number)
            continue
        timestamp = _parse_timestamp(value["timestamp"])
        if timestamp is None:
            _error(parsed.errors, "timestamp_invalid", "timestamp must be RFC3339 with an explicit offset", line=line_number)
            continue
        if event_type != "message":
            _error(parsed.errors, "unsupported_event_type", "v1 snapshot input accepts event_type=message only", line=line_number)
            continue
        if source_revision is not None and (not isinstance(source_revision, str) or not source_revision):
            _error(parsed.errors, "schema_invalid", "source_revision must be a non-empty string when present", line=line_number)
            continue
        if not isinstance(metadata, dict):
            _error(parsed.errors, "schema_invalid", "metadata must be an object", line=line_number)
            continue
        attachments, attachment_error = _normalize_attachments(value["attachments"])
        if attachment_error:
            _error(parsed.errors, "attachment_invalid", attachment_error, line=line_number)
            continue
        assert attachments is not None
        normalized_payload = {
            "conversation_id": conversation_id,
            "message_id": message_id,
            "role": role,
            "timestamp_utc": timestamp[1],
            "sequence": sequence,
            "text": text,
            "attachments": list(attachments),
        }
        source_unit_hash = sha256_json(normalized_payload)
        revision_id = source_unit_hash[:16]
        source_unit_id = "su_" + sha256_json([source_id, conversation_id, message_id, revision_id])[:32]
        parsed.messages.append(
            Message(
                conversation_id=conversation_id,
                message_id=message_id,
                role=role,
                timestamp=timestamp[0],
                timestamp_utc=timestamp[1],
                sequence=sequence,
                text=text,
                attachments=attachments,
                event_type=event_type,
                source_revision=source_revision,
                metadata=metadata,
                source_unit_hash=source_unit_hash,
                revision_id=revision_id,
                source_unit_id=source_unit_id,
            )
        )

    parsed.messages.sort(key=lambda item: (item.conversation_id, item.sequence, item.timestamp_utc, item.message_id))
    if len(parsed.messages) > max_messages:
        _error(parsed.errors, "message_limit_exceeded", "input exceeds the configured message limit")
    if not parsed.messages and not parsed.errors:
        _error(parsed.errors, "input_empty", "input contains no messages")
    _validate_unique_and_contiguous(parsed)
    return parsed


def _validate_unique_and_contiguous(parsed: ParsedInput) -> None:
    by_conversation: dict[str, list[Message]] = {}
    for message in parsed.messages:
        by_conversation.setdefault(message.conversation_id, []).append(message)
    for conversation_id, messages in sorted(by_conversation.items()):
        message_ids = [item.message_id for item in messages]
        if len(message_ids) != len(set(message_ids)):
            _error(parsed.errors, "message_id_duplicate", f"conversation {conversation_id} contains duplicate message_id values")
        sequences = [item.sequence for item in messages]
        if len(sequences) != len(set(sequences)):
            _error(parsed.errors, "sequence_duplicate", f"conversation {conversation_id} contains duplicate sequence values")
        for previous, current in zip(messages, messages[1:]):
            if current.sequence != previous.sequence + 1:
                _error(
                    parsed.errors,
                    "sequence_gap",
                    f"conversation {conversation_id} sequence is not contiguous",
                )
                break