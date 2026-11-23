from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .db import SQLiteStore, utc_now_ts


class IncrementalIngestionError(RuntimeError):
    """Base error for the raw-only ingestion path."""


class ConfigValidationError(IncrementalIngestionError):
    pass


class InputValidationError(IncrementalIngestionError):
    pass


class PolicyViolation(IncrementalIngestionError):
    pass


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_text(value: str) -> str:
    return _sha256_bytes(value.encode("utf-8"))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _is_type(value: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    return True


def _resolve_ref(root: dict[str, Any], ref: str) -> dict[str, Any]:
    if not ref.startswith("#/"):
        raise ConfigValidationError(f"unsupported schema reference: {ref}")
    node: Any = root
    for token in ref[2:].split("/"):
        token = token.replace("~1", "/").replace("~0", "~")
        if not isinstance(node, dict) or token not in node:
            raise ConfigValidationError(f"unresolvable schema reference: {ref}")
        node = node[token]
    if not isinstance(node, dict):
        raise ConfigValidationError(f"schema reference is not an object: {ref}")
    return node


def validate_json_schema(instance: Any, schema: dict[str, Any], *, root: dict[str, Any] | None = None, path: str = "$") -> None:
    """Validate the JSON-Schema subset used by the Step 2 config without third-party packages."""

    root = root or schema
    if "$ref" in schema:
        validate_json_schema(instance, _resolve_ref(root, schema["$ref"]), root=root, path=path)
        return
    if "type" in schema:
        expected = schema["type"]
        if isinstance(expected, str) and not _is_type(instance, expected):
            raise ConfigValidationError(f"{path}: expected {expected}, got {type(instance).__name__}")
    if "const" in schema and instance != schema["const"]:
        raise ConfigValidationError(f"{path}: value does not match required const")
    if "enum" in schema and instance not in schema["enum"]:
        raise ConfigValidationError(f"{path}: value is not in enum")
    if isinstance(instance, str):
        if "minLength" in schema and len(instance) < schema["minLength"]:
            raise ConfigValidationError(f"{path}: string is shorter than {schema['minLength']}")
        if "pattern" in schema and re.search(schema["pattern"], instance) is None:
            raise ConfigValidationError(f"{path}: string does not match pattern {schema['pattern']}")
    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            raise ConfigValidationError(f"{path}: number is below minimum {schema['minimum']}")
    if isinstance(instance, list):
        if "minItems" in schema and len(instance) < schema["minItems"]:
            raise ConfigValidationError(f"{path}: array has fewer than {schema['minItems']} items")
        if "maxItems" in schema and len(instance) > schema["maxItems"]:
            raise ConfigValidationError(f"{path}: array has more than {schema['maxItems']} items")
        if schema.get("uniqueItems"):
            keys = [_compact_json(item) for item in instance]
            if len(set(keys)) != len(keys):
                raise ConfigValidationError(f"{path}: array items are not unique")
        if "items" in schema:
            for index, item in enumerate(instance):
                validate_json_schema(item, schema["items"], root=root, path=f"{path}[{index}]")
    if isinstance(instance, dict):
        for key in schema.get("required", []):
            if key not in instance:
                raise ConfigValidationError(f"{path}: missing required field {key}")
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            unknown = sorted(set(instance) - set(properties))
            if unknown:
                raise ConfigValidationError(f"{path}: unknown fields: {', '.join(unknown)}")
        for key, value in instance.items():
            if key in properties:
                validate_json_schema(value, properties[key], root=root, path=f"{path}.{key}")


@dataclass
class IngestionConfig:
    data: dict[str, Any]
    project_root: Path
    config_path: Path
    schema_path: Path

    @property
    def runtime(self) -> dict[str, Any]:
        return self.data["runtime"]

    @property
    def allowlist(self) -> list[dict[str, Any]]:
        return self.data["workspace_allowlist"]

    @property
    def incremental_root(self) -> Path:
        return (self.project_root / "work" / "incremental_ingestion_v1").resolve()

    def resolve_path(self, raw: str | Path) -> Path:
        value = str(raw)
        if ".." in Path(value).parts:
            raise PolicyViolation(f"path traversal is forbidden: {value}")
        path = Path(value)
        if not path.is_absolute():
            path = self.project_root / path
        return path.resolve()

    def validate_runtime_paths(self) -> None:
        root = self.incremental_root
        for key, value in self.runtime.items():
            resolved = self.resolve_path(value)
            if resolved != root and root not in resolved.parents:
                raise PolicyViolation(f"runtime path escapes isolated work root: {key}={value}")
        expected_work = (self.project_root / "work").resolve()
        for entry in self.allowlist:
            for value in entry["input_roots"]:
                resolved = self.resolve_path(value)
                if resolved != expected_work and expected_work not in resolved.parents:
                    raise PolicyViolation(f"input root escapes project work tree: {value}")
            for value in entry["allowed_source_files"]:
                resolved = self.resolve_path(value)
                if not any(
                    resolved == self.resolve_path(root_value) or self.resolve_path(root_value) in resolved.parents
                    for root_value in entry["input_roots"]
                ):
                    raise PolicyViolation(f"allowed source file is outside input root: {value}")

    def validate_semantics(self) -> None:
        if self.data["mode"] != "manual_only":
            raise PolicyViolation("Step 2 only supports manual_only mode")
        if self.data["dry_run_default"] is not True:
            raise PolicyViolation("dry_run_default must remain true")
        policy = self.data["input_policy"]
        if policy["project_id_policy"] != "explicit_required_no_default_or_fallback":
            raise PolicyViolation("project_id must be explicit with no default fallback")
        if policy["source_unit_id_rule"] != '"su_" + sha256(source_id|conversation_id|message_id|revision_id)[:32]':
            raise PolicyViolation("source_unit_id rule mismatch")
        if policy["source_unit_scope_ref_rule"] != "<message_id>#<revision_id>":
            raise PolicyViolation("scope_ref rule mismatch")
        if self.data["promotion_policy"]["auto_promote_allowlist"]:
            raise PolicyViolation("auto promotion must remain empty in the first phase")
        if self.data["promotion_policy"]["rollout_mode"] != "manual_review_first":
            raise PolicyViolation("first phase must be manual review first")
        if self.data["promotion_policy"]["auto_promotion_status"] != "deferred_not_implemented":
            raise PolicyViolation("automatic promotion must remain deferred")
        safety = self.data["safety"]
        dangerous = (
            "llm_allowed", "embedding_allowed", "canonical_write_allowed", "milvus_write_allowed",
            "alias_mutation_allowed", "production_alias_allowed", "scheduler_allowed", "full_workspace_scan_allowed",
        )
        if any(safety.get(key) is not False for key in dangerous):
            raise PolicyViolation("unsafe Step 2 switch is enabled")
        composites: set[tuple[str, str, str, str]] = set()
        for entry in self.allowlist:
            compact = (entry["workspace_id"], entry["project_id"], entry["domain_profile"], entry["source_adapter"])
            if compact in composites:
                raise ConfigValidationError("duplicate workspace/project/domain/adapter allowlist entry")
            composites.add(compact)
            if not entry["workspace_id"].strip() or not entry["project_id"].strip():
                raise ConfigValidationError("workspace_id and project_id must be non-empty")
        self.validate_runtime_paths()

    def select_entry(self, input_path: Path) -> dict[str, Any]:
        resolved_input = input_path.resolve()
        matches: list[dict[str, Any]] = []
        for entry in self.allowlist:
            for allowed in entry["allowed_source_files"]:
                if self.resolve_path(allowed) == resolved_input:
                    matches.append(entry)
                    break
        if len(matches) != 1:
            raise PolicyViolation("input file must match exactly one allowlist entry")
        if matches[0]["enabled"] is not True:
            raise PolicyViolation("matching allowlist entry is disabled")
        return matches[0]

    def validate_db_path(self, db_path: Path) -> Path:
        resolved = db_path.resolve()
        root = self.incremental_root
        if resolved != root and root not in resolved.parents:
            raise PolicyViolation("database must be under work/incremental_ingestion_v1")
        formal = (self.project_root / "work" / "conversation_cleaning_v1" / "canonical" / "pilot_memory.sqlite3").resolve()
        if resolved == formal:
            raise PolicyViolation("the formal pilot canonical database is read-only")
        return resolved


def load_config(config_path: str | Path, *, project_root: str | Path | None = None, schema_path: str | Path | None = None) -> IngestionConfig:
    config_file = Path(config_path).resolve()
    root = Path(project_root).resolve() if project_root is not None else Path(__file__).resolve().parents[2]
    schema_file = Path(schema_path).resolve() if schema_path is not None else root / "config" / "incremental_ingestion_v1.schema.json"
    if not config_file.is_file():
        raise ConfigValidationError(f"config file not found: {config_file}")
    if not schema_file.is_file():
        raise ConfigValidationError(f"schema file not found: {schema_file}")
    try:
        data = json.loads(config_file.read_text(encoding="utf-8"))
        schema = json.loads(schema_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigValidationError(f"invalid JSON: {exc}") from exc
    validate_json_schema(data, schema, root=schema)
    config = IngestionConfig(data=data, project_root=root, config_path=config_file, schema_path=schema_file)
    config.validate_semantics()
    return config

@dataclass(frozen=True)
class SourceDescriptor:
    source_id: str
    source_path: str
    source_sha256: str
    source_revision_id: str
    manifest_path: str | None
    manifest_sha256: str | None
    workspace_id: str
    project_id: str
    owner_principal_id: str | None
    source_type: str
    visibility: str
    sensitivity: str


@dataclass(frozen=True)
class ParsedMessage:
    raw_line_number: int
    raw_row_sha256: str
    event_type: str
    conversation_id: str | None
    message_id: str | None
    role: str | None
    sequence: int | None
    timestamp_utc: str | None
    text: str | None
    attachments: tuple[dict[str, Any], ...]
    metadata: dict[str, Any]
    source_unit_hash: str | None
    revision_id: str | None
    source_unit_id: str | None
    scope_ref: str | None
    error_code: str | None = None
    error_message: str | None = None
    attachment_review: bool = False


def _parse_error(line_number: int, raw_hash: str, code: str, message: str,
                 *, conversation_id: str | None = None) -> ParsedMessage:
    return ParsedMessage(
        raw_line_number=line_number, raw_row_sha256=raw_hash, event_type="message",
        conversation_id=conversation_id, message_id=None, role=None, sequence=None, timestamp_utc=None,
        text=None, attachments=(), metadata={}, source_unit_hash=None, revision_id=None,
        source_unit_id=None, scope_ref=None, error_code=code, error_message=message,
    )


def _normalize_timestamp(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("timestamp must be a string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("timestamp is not RFC3339 parseable") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must include an offset")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _attachment_review_required(attachments: list[dict[str, Any]]) -> bool:
    for attachment in attachments:
        if attachment.get("extraction_status") != "extracted":
            return True
        if not attachment.get("attachment_id") or not attachment.get("extracted_text_path"):
            return True
    return False


def _parse_row(row: Any, *, line_number: int, raw_hash: str, source_id: str, entry: dict[str, Any]) -> ParsedMessage:
    if not isinstance(row, dict):
        return _parse_error(line_number, raw_hash, "schema_invalid", "JSONL row must be an object")
    metadata = row.get("metadata", {})
    if not isinstance(metadata, dict):
        return _parse_error(line_number, raw_hash, "schema_invalid", "metadata must be an object")
    for field in ("conversation_id", "message_id", "role", "timestamp", "sequence", "text", "attachments"):
        if field not in row:
            return _parse_error(line_number, raw_hash, "schema_invalid", f"missing required field: {field}")
    event_type = row.get("event_type", "message")
    if event_type not in {"message", "edit", "delete"}:
        return _parse_error(line_number, raw_hash, "schema_invalid", "unsupported event_type")
    conversation_id = row.get("conversation_id")
    if not isinstance(conversation_id, str) or not conversation_id:
        return _parse_error(line_number, raw_hash, "schema_invalid", "conversation_id must be non-empty")
    if conversation_id not in set(entry["allowed_conversation_ids"]):
        return _parse_error(line_number, raw_hash, "non_allowlisted_conversation", "conversation is not allowlisted", conversation_id=conversation_id)
    declared_workspace = row.get("workspace_id", metadata.get("workspace_id"))
    declared_project = row.get("project_id", metadata.get("project_id"))
    if declared_workspace is not None and declared_workspace != entry["workspace_id"]:
        return _parse_error(line_number, raw_hash, "unknown_workspace", "workspace is outside allowlist", conversation_id=conversation_id)
    if declared_project is not None and declared_project != entry["project_id"]:
        return _parse_error(line_number, raw_hash, "unknown_project", "project is outside allowlist", conversation_id=conversation_id)
    message_id = row.get("message_id")
    if not isinstance(message_id, str) or not message_id:
        return _parse_error(line_number, raw_hash, "schema_invalid", "message_id must be non-empty")
    role = row.get("role")
    if role not in set(entry["allowed_roles"]):
        return _parse_error(line_number, raw_hash, "schema_invalid", "role is not allowed")
    sequence = row.get("sequence")
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 0:
        return _parse_error(line_number, raw_hash, "schema_invalid", "sequence must be a non-negative integer")
    try:
        timestamp_utc = _normalize_timestamp(row.get("timestamp"))
    except ValueError as exc:
        return _parse_error(line_number, raw_hash, "timezone_ambiguity", str(exc))
    text = row.get("text")
    if not isinstance(text, str):
        return _parse_error(line_number, raw_hash, "schema_invalid", "text must be a string")
    attachments = row.get("attachments")
    if not isinstance(attachments, list) or any(not isinstance(item, dict) for item in attachments):
        return _parse_error(line_number, raw_hash, "schema_invalid", "attachments must be an array of objects")
    if event_type == "delete":
        canonical = {"conversation_id": conversation_id, "event_type": event_type, "message_id": message_id,
                     "sequence": sequence, "timestamp_utc": timestamp_utc, "text": text}
        source_unit_id = None
    else:
        canonical = {"attachments": attachments, "conversation_id": conversation_id, "message_id": message_id,
                     "role": role, "sequence": sequence, "text": text, "timestamp_utc": timestamp_utc}
        source_unit_id = None
    source_unit_hash = _sha256_text(_compact_json(canonical))
    revision_id = source_unit_hash[:16]
    if event_type != "delete":
        source_unit_id = "su_" + _sha256_text(f"{source_id}|{conversation_id}|{message_id}|{revision_id}")[:32]
    return ParsedMessage(
        raw_line_number=line_number, raw_row_sha256=raw_hash, event_type=event_type,
        conversation_id=conversation_id, message_id=message_id, role=role, sequence=sequence,
        timestamp_utc=timestamp_utc, text=text, attachments=tuple(attachments), metadata=metadata,
        source_unit_hash=source_unit_hash, revision_id=revision_id, source_unit_id=source_unit_id,
        scope_ref=f"{message_id}#{revision_id}",
        attachment_review=_attachment_review_required(attachments) if event_type != "delete" else False,
    )


def parse_jsonl(path: Path, *, source_id: str, entry: dict[str, Any]) -> list[ParsedMessage]:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise InputValidationError("input is not valid UTF-8") from exc
    lines = text.splitlines()
    if len(lines) > entry["max_messages_per_run"]:
        raise InputValidationError("input exceeds max_messages_per_run")
    records: list[ParsedMessage] = []
    for line_number, line in enumerate(lines, start=1):
        raw_hash = _sha256_text(line)
        if not line.strip():
            records.append(_parse_error(line_number, raw_hash, "schema_invalid", "blank JSONL line"))
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            records.append(_parse_error(line_number, raw_hash, "schema_invalid", "invalid JSON"))
            continue
        records.append(_parse_row(row, line_number=line_number, raw_hash=raw_hash, source_id=source_id, entry=entry))
    conversations = {row.conversation_id for row in records if row.conversation_id is not None}
    if len(conversations) > entry["max_conversations_per_run"]:
        raise InputValidationError("input exceeds max_conversations_per_run")
    previous_sequence: int | None = None
    out_of_order = False
    for row in records:
        if row.sequence is None:
            continue
        if previous_sequence is not None and row.sequence < previous_sequence:
            out_of_order = True
            break
        previous_sequence = row.sequence
    if out_of_order:
        records = [replace(row, error_code="out_of_order", error_message="physical input sequence moved backwards")
                   if row.error_code is None else row for row in records]
    by_sequence: dict[int, list[int]] = defaultdict(list)
    for index, row in enumerate(records):
        if row.error_code is None and row.sequence is not None:
            by_sequence[row.sequence].append(index)
    for sequence, indexes in by_sequence.items():
        signatures = {(records[index].message_id, records[index].revision_id, records[index].event_type)
                      for index in indexes}
        if len(signatures) > 1:
            for index in indexes:
                records[index] = replace(records[index], error_code="sequence_conflict",
                                         error_message=f"sequence {sequence} maps to different message revisions")
    return records


def _resolve_source(config: IngestionConfig, entry: dict[str, Any], input_path: Path, input_sha256: str) -> SourceDescriptor:
    manifest_path = input_path.parent / "source_manifest.json"
    manifest_sha256: str | None = None
    source_type = "conversation_jsonl"
    visibility = entry["allowed_visibilities"][0]
    sensitivity = entry["default_sensitivity"]
    owner = entry["owner_principal_id"]
    if manifest_path.is_file():
        manifest_sha256 = _sha256_file(manifest_path)
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise InputValidationError("source manifest is not valid JSON") from exc
        entries = manifest.get("entries", [])
        if not isinstance(entries, list):
            raise InputValidationError("source manifest entries must be an array")
        matches = []
        for candidate in entries:
            if not isinstance(candidate, dict):
                continue
            uri = candidate.get("source_uri")
            if isinstance(uri, str):
                try:
                    if Path(uri).resolve() == input_path.resolve():
                        matches.append(candidate)
                except OSError:
                    pass
            if candidate.get("source_sha256") == input_sha256 and candidate not in matches:
                matches.append(candidate)
        if len(matches) != 1:
            raise InputValidationError("source manifest must contain exactly one matching source")
        manifest_entry = matches[0]
        if manifest_entry.get("source_sha256") != input_sha256:
            raise InputValidationError("source hash does not match source manifest")
        if manifest_entry.get("workspace_id") != entry["workspace_id"] or manifest_entry.get("project_id") != entry["project_id"]:
            raise PolicyViolation("source manifest workspace/project does not match allowlist")
        owner_value = manifest_entry.get("owner_principal_id")
        if owner_value is not None:
            owner = owner_value
        if entry["owner_principal_id"] is not None and owner != entry["owner_principal_id"]:
            raise PolicyViolation("source manifest owner does not match allowlist")
        source_id = manifest_entry.get("source_id")
        if not isinstance(source_id, str) or not source_id:
            raise InputValidationError("source manifest source_id is missing")
        source_type = manifest_entry.get("source_type", source_type)
        manifest_visibility = manifest_entry.get("visibility")
        if manifest_visibility is not None:
            if manifest_visibility not in entry["allowed_visibilities"]:
                raise PolicyViolation("source manifest visibility is not allowed")
            visibility = manifest_visibility
        manifest_sensitivity = manifest_entry.get("sensitivity")
        if manifest_sensitivity is not None:
            sensitivity = manifest_sensitivity
    else:
        relative = input_path.relative_to(config.project_root).as_posix()
        source_id = "src_" + _sha256_text(
            f"{entry['workspace_id']}|{entry['project_id']}|{entry['source_adapter']}|{relative}"
        )[:24]
    source_revision_id = "srev_" + _sha256_text(f"{source_id}|{input_sha256}")[:32]
    return SourceDescriptor(
        source_id=source_id, source_path=str(input_path), source_sha256=input_sha256,
        source_revision_id=source_revision_id,
        manifest_path=str(manifest_path) if manifest_path.is_file() else None,
        manifest_sha256=manifest_sha256, workspace_id=entry["workspace_id"],
        project_id=entry["project_id"], owner_principal_id=owner, source_type=source_type,
        visibility=visibility, sensitivity=sensitivity,
    )

def _cursor_id(workspace_id: str, project_id: str, source_id: str, conversation_id: str) -> str:
    return "cur_" + _sha256_text(f"{workspace_id}|{project_id}|{source_id}|{conversation_id}")[:32]


def _record_id(run_id: str, line_number: int) -> str:
    return "irec_" + _sha256_text(f"{run_id}|{line_number}")[:32]


def _event_id(source_id: str, conversation_id: str, message_id: str, revision_id: str, event_type: str) -> str:
    return "mev_" + _sha256_text(f"{source_id}|{conversation_id}|{message_id}|{revision_id}|{event_type}")[:32]


def _default_cursor(workspace_id: str, project_id: str, source_id: str, conversation_id: str) -> dict[str, Any]:
    return {
        "cursor_id": _cursor_id(workspace_id, project_id, source_id, conversation_id),
        "workspace_id": workspace_id, "project_id": project_id, "source_id": source_id,
        "conversation_id": conversation_id, "max_observed_sequence": -1,
        "last_contiguous_committed_sequence": -1, "last_committed_message_id": None,
        "last_committed_source_unit_hash": None, "source_sha256_at_cursor": "",
        "status": "active", "updated_at": 0,
    }


def _load_cursor(connection: sqlite3.Connection, workspace_id: str, project_id: str, source_id: str, conversation_id: str) -> dict[str, Any]:
    row = connection.execute(
        """SELECT * FROM ingestion_cursors WHERE workspace_id=? AND project_id=? AND source_id=?
           AND conversation_id=?""",
        (workspace_id, project_id, source_id, conversation_id),
    ).fetchone()
    return dict(row) if row else _default_cursor(workspace_id, project_id, source_id, conversation_id)


def _committed_events(connection: sqlite3.Connection, source_id: str, conversation_id: str) -> dict[int, dict[str, Any]]:
    rows = connection.execute(
        """SELECT sequence,message_id,source_unit_hash,revision_id,event_type
           FROM message_revision_events WHERE source_id=? AND conversation_id=?
           ORDER BY created_at,event_id""",
        (source_id, conversation_id),
    ).fetchall()
    result: dict[int, dict[str, Any]] = {}
    for row in rows:
        value = dict(row)
        if value["sequence"] >= 0:
            result[value["sequence"]] = value
    return result


def _counts(connection: sqlite3.Connection) -> dict[str, int]:
    def count(table: str, where: str = "") -> int:
        found = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        if not found:
            return 0
        return int(connection.execute(f"SELECT COUNT(*) AS n FROM {table} {where}").fetchone()["n"])
    return {
        "canonical_active": count("memory_items", "WHERE status='active' AND deleted_at IS NULL"),
        "canonical_total": count("memory_items"),
        "outbox_total": count("memory_outbox"),
    }


def _write_run(connection: sqlite3.Connection, *, run_id: str, batch_id: str, source: SourceDescriptor, input_path: Path, source_sha256: str, mode: str, started_at: int) -> None:
    connection.execute(
        """INSERT INTO ingestion_runs(
           run_id,batch_id,workspace_id,project_id,source_id,source_revision_id,source_path,
           source_sha256,mode,status,started_at,cursor_before_json,cursor_after_json,summary_json,error_json)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (run_id, batch_id, source.workspace_id, source.project_id, source.source_id, source.source_revision_id,
         str(input_path), source_sha256, mode, "running", started_at, "{}", "{}", "{}", "{}"),
    )


def _write_batch(connection: sqlite3.Connection, *, run_id: str, batch_id: str, source: SourceDescriptor, conversation_id: str, created_at: int) -> None:
    connection.execute(
        """INSERT INTO ingestion_batches(
           batch_id,run_id,workspace_id,project_id,source_id,source_revision_id,conversation_id,status,
           accepted_count,duplicate_count,quarantine_count,failed_count,attachment_review_count,created_at,summary_json)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (batch_id, run_id, source.workspace_id, source.project_id, source.source_id, source.source_revision_id,
         conversation_id, "running", 0, 0, 0, 0, 0, created_at, "{}"),
    )


def _ensure_source(connection: sqlite3.Connection, source: SourceDescriptor, input_path: Path) -> None:
    existing = connection.execute("SELECT * FROM sources WHERE source_id=?", (source.source_id,)).fetchone()
    if existing:
        value = dict(existing)
        if (value["workspace_id"] != source.workspace_id or value["project_id"] != source.project_id
                or (value["owner_principal_id"] or None) != (source.owner_principal_id or None)):
            raise PolicyViolation("existing source scope does not match allowlist")
        connection.execute(
            """UPDATE sources SET source_path=?,source_sha256=?,source_type=?,sensitivity=?,visibility=?
               WHERE source_id=?""",
            (str(input_path), source.source_sha256, source.source_type, source.sensitivity, source.visibility, source.source_id),
        )
    else:
        connection.execute(
            """INSERT INTO sources(source_id,workspace_id,project_id,visibility,owner_principal_id,source_path,
               source_sha256,source_type,sensitivity,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (source.source_id, source.workspace_id, source.project_id, source.visibility, source.owner_principal_id,
             str(input_path), source.source_sha256, source.source_type, source.sensitivity, utc_now_ts()),
        )
    existing_revision = connection.execute(
        "SELECT source_sha256 FROM source_revisions WHERE source_revision_id=?", (source.source_revision_id,)
    ).fetchone()
    if existing_revision and dict(existing_revision)["source_sha256"] != source.source_sha256:
        raise InputValidationError("source revision hash conflict")
    if not existing_revision:
        connection.execute(
            """INSERT INTO source_revisions(source_revision_id,workspace_id,project_id,source_id,source_path,
               source_sha256,source_type,manifest_path,manifest_sha256,imported_at,metadata_json)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (source.source_revision_id, source.workspace_id, source.project_id, source.source_id, str(input_path),
             source.source_sha256, source.source_type, source.manifest_path, source.manifest_sha256,
             utc_now_ts(), "{}"),
        )


def _existing_event(connection: sqlite3.Connection, row: ParsedMessage, source: SourceDescriptor) -> dict[str, Any] | None:
    assert row.message_id is not None and row.revision_id is not None and row.conversation_id is not None
    event = connection.execute(
        """SELECT * FROM message_revision_events
           WHERE source_id=? AND conversation_id=? AND message_id=? AND revision_id=?""",
        (source.source_id, row.conversation_id, row.message_id, row.revision_id),
    ).fetchone()
    return dict(event) if event else None


def _sequence_messages(connection: sqlite3.Connection, row: ParsedMessage, source: SourceDescriptor) -> set[str]:
    if row.sequence is None or row.conversation_id is None:
        return set()
    rows = connection.execute(
        """SELECT DISTINCT message_id FROM message_revision_events
           WHERE source_id=? AND conversation_id=? AND sequence=?""",
        (source.source_id, row.conversation_id, row.sequence),
    ).fetchall()
    return {str(value["message_id"]) for value in rows}


def _sequence_has_other_message(connection: sqlite3.Connection, row: ParsedMessage, source: SourceDescriptor) -> bool:
    if row.message_id is None:
        return False
    return any(message_id != row.message_id for message_id in _sequence_messages(connection, row, source))


def _commit_event(connection: sqlite3.Connection, row: ParsedMessage, source: SourceDescriptor, source_revision_id: str) -> None:
    assert row.message_id is not None and row.conversation_id is not None and row.sequence is not None
    if row.source_unit_id is not None:
        existing_unit = connection.execute(
            "SELECT source_unit_hash,project_id FROM source_units WHERE source_unit_id=?", (row.source_unit_id,)
        ).fetchone()
        if existing_unit:
            value = dict(existing_unit)
            if value["source_unit_hash"] != row.source_unit_hash or value["project_id"] != source.project_id:
                raise InputValidationError("source unit hash/project conflict")
        else:
            metadata = {
                "conversation_id": row.conversation_id, "event_type": row.event_type,
                "message_id": row.message_id, "role": row.role, "sequence": row.sequence,
                "timestamp_utc": row.timestamp_utc, "revision_id": row.revision_id,
                "source_revision_id": source_revision_id, "attachments": list(row.attachments),
                "attachment_review": row.attachment_review,
            }
            connection.execute(
                """INSERT INTO source_units(
                   source_unit_id,source_id,project_id,parent_unit_id,scope_type,scope_ref,source_unit_hash,
                   text_content,metadata_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (row.source_unit_id, source.source_id, source.project_id, None, "message", row.scope_ref,
                 row.source_unit_hash, row.text or "", _compact_json(metadata), utc_now_ts()),
            )
    event_id = _event_id(source.source_id, row.conversation_id, row.message_id, row.revision_id or "", row.event_type)
    connection.execute(
        """INSERT OR IGNORE INTO message_revision_events(
           event_id,workspace_id,project_id,source_id,conversation_id,message_id,sequence,revision_id,
           event_type,source_unit_id,source_unit_hash,scope_ref,created_at,metadata_json)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (event_id, source.workspace_id, source.project_id, source.source_id, row.conversation_id, row.message_id,
         row.sequence, row.revision_id, row.event_type, row.source_unit_id, row.source_unit_hash, row.scope_ref,
         utc_now_ts(), _compact_json({"source_revision_id": source_revision_id, "attachment_review": row.attachment_review})),
    )


def _insert_record(connection: sqlite3.Connection, *, run_id: str, batch_id: str, source: SourceDescriptor, row: ParsedMessage, status: str, raw_committed: bool) -> None:
    connection.execute(
        """INSERT INTO ingestion_records(
           record_id,run_id,batch_id,workspace_id,project_id,source_id,source_revision_id,conversation_id,
           message_id,sequence,event_type,revision_id,source_unit_id,source_unit_hash,raw_line_number,
           raw_row_sha256,record_status,raw_committed,review_status,error_code,error_message,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (_record_id(run_id, row.raw_line_number), run_id, batch_id, source.workspace_id, source.project_id,
         source.source_id, source.source_revision_id, row.conversation_id, row.message_id, row.sequence,
         row.event_type, row.revision_id, row.source_unit_id, row.source_unit_hash, row.raw_line_number,
         row.raw_row_sha256, status, 1 if raw_committed else 0,
         "attachment_review" if row.attachment_review else "none", row.error_code, row.error_message, utc_now_ts()),
    )


def _update_cursor(connection: sqlite3.Connection, *, source: SourceDescriptor, conversation_id: str, max_observed: int, quarantine_count: int = 0) -> dict[str, Any]:
    committed = _committed_events(connection, source.source_id, conversation_id)
    last = -1
    last_info: dict[str, Any] | None = None
    while last + 1 in committed:
        last += 1
        last_info = committed[last]
    missing = [value for value in range(0, max_observed + 1) if value not in committed]
    if not committed and (max_observed >= 0 or quarantine_count > 0):
        status = "quarantined"
    elif missing:
        status = "partial"
    else:
        status = "active"
    cursor = _default_cursor(source.workspace_id, source.project_id, source.source_id, conversation_id)
    cursor.update({
        "max_observed_sequence": max_observed, "last_contiguous_committed_sequence": last,
        "last_committed_message_id": last_info["message_id"] if last_info else None,
        "last_committed_source_unit_hash": last_info["source_unit_hash"] if last_info else None,
        "source_sha256_at_cursor": source.source_sha256, "status": status, "updated_at": utc_now_ts(),
    })
    connection.execute(
        """INSERT INTO ingestion_cursors(
           cursor_id,workspace_id,project_id,source_id,conversation_id,max_observed_sequence,
           last_contiguous_committed_sequence,last_committed_message_id,last_committed_source_unit_hash,
           source_sha256_at_cursor,status,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(cursor_id) DO UPDATE SET
             max_observed_sequence=excluded.max_observed_sequence,
             last_contiguous_committed_sequence=excluded.last_contiguous_committed_sequence,
             last_committed_message_id=excluded.last_committed_message_id,
             last_committed_source_unit_hash=excluded.last_committed_source_unit_hash,
             source_sha256_at_cursor=excluded.source_sha256_at_cursor,
             status=excluded.status,updated_at=excluded.updated_at""",
        (cursor["cursor_id"], cursor["workspace_id"], cursor["project_id"], cursor["source_id"],
         cursor["conversation_id"], cursor["max_observed_sequence"], cursor["last_contiguous_committed_sequence"],
         cursor["last_committed_message_id"], cursor["last_committed_source_unit_hash"],
         cursor["source_sha256_at_cursor"], cursor["status"], cursor["updated_at"]),
    )
    return cursor

def _summary_base(*, mode: str, input_path: Path, db_path: Path, source: SourceDescriptor,
                  rows: list[ParsedMessage], source_sha256_before: str, source_sha256_after: str,
                  manifest_sha256_before: str | None, manifest_sha256_after: str | None) -> dict[str, Any]:
    quarantine = Counter(row.error_code for row in rows if row.error_code)
    return {
        "status": "dry_run" if mode == "dry_run" else "running", "mode": mode,
        "input": str(input_path), "db": str(db_path), "workspace_id": source.workspace_id,
        "project_id": source.project_id, "source_id": source.source_id,
        "source_revision_id": source.source_revision_id,
        "source_sha256_before": source_sha256_before, "source_sha256_after": source_sha256_after,
        "source_hash_unchanged": source_sha256_before == source_sha256_after,
        "manifest_path": source.manifest_path, "manifest_sha256_before": manifest_sha256_before,
        "manifest_sha256_after": manifest_sha256_after,
        "manifest_hash_unchanged": manifest_sha256_before == manifest_sha256_after,
        "input_count": len(rows), "would_accept_count": sum(1 for row in rows if row.error_code is None),
        "quarantine_count": sum(1 for row in rows if row.error_code is not None),
        "quarantine_breakdown": dict(sorted(quarantine.items())),
        "attachment_review_count": sum(1 for row in rows if row.error_code is None and row.attachment_review),
        "sequence_min": min((row.sequence for row in rows if row.sequence is not None), default=None),
        "sequence_max": max((row.sequence for row in rows if row.sequence is not None), default=None),
        "db_write": False, "llm_called": False, "embedding_called": False,
        "canonical_write": False, "outbox_write": False, "milvus_write": False, "alias_change": False,
    }


def run_ingestion(*, config: IngestionConfig, input_path: str | Path, db_path: str | Path,
                  mode: str = "dry_run", max_records: int | None = None,
                  run_id: str | None = None) -> dict[str, Any]:
    if mode not in {"dry_run", "apply"}:
        raise PolicyViolation("mode must be dry_run or apply")
    if max_records is not None and max_records < 1:
        raise PolicyViolation("max_records must be positive")
    resolved_input = config.resolve_path(input_path)
    resolved_db = config.validate_db_path(config.resolve_path(db_path))
    if not resolved_input.is_file():
        raise InputValidationError(f"input file not found: {resolved_input}")
    entry = config.select_entry(resolved_input)
    source_sha_before = _sha256_file(resolved_input)
    manifest_candidate = resolved_input.parent / "source_manifest.json"
    manifest_sha_before = _sha256_file(manifest_candidate) if manifest_candidate.is_file() else None
    source = _resolve_source(config, entry, resolved_input, source_sha_before)
    rows = parse_jsonl(resolved_input, source_id=source.source_id, entry=entry)
    source_sha_after = _sha256_file(resolved_input)
    manifest_sha_after = _sha256_file(manifest_candidate) if manifest_candidate.is_file() else None
    if source_sha_before != source_sha_after:
        raise InputValidationError("input changed while being read")
    if manifest_sha_before != manifest_sha_after:
        raise InputValidationError("source manifest changed while being read")
    summary = _summary_base(
        mode=mode, input_path=resolved_input, db_path=resolved_db, source=source, rows=rows,
        source_sha256_before=source_sha_before, source_sha256_after=source_sha_after,
        manifest_sha256_before=manifest_sha_before, manifest_sha256_after=manifest_sha_after,
    )
    pilot = config.data["pilot"]
    if config.resolve_path(pilot["input_file"]) == resolved_input:
        if pilot["expected_source_sha256"] != source_sha_before:
            raise InputValidationError("pilot source hash mismatch")
        if pilot["expected_message_count"] != len(rows):
            raise InputValidationError("pilot message count mismatch")
    if summary["quarantine_count"] and summary["would_accept_count"] == 0 and mode == "dry_run":
        summary["status"] = "quarantined"
    if mode == "dry_run":
        return summary

    run_id = run_id or "run-" + uuid.uuid4().hex[:20]
    batch_id = "batch-" + uuid.uuid4().hex[:20]
    sorted_rows = sorted(rows, key=lambda row: (row.sequence is None, row.sequence or 0, row.raw_line_number))
    selected = sorted_rows[:max_records] if max_records is not None else sorted_rows
    remaining = len(sorted_rows) - len(selected)
    started_at = utc_now_ts()
    db_store = SQLiteStore(resolved_db)
    db_store.migrate()

    with db_store.transaction() as connection:
        connection.execute(
            "INSERT OR IGNORE INTO workspaces(workspace_id,name,created_at) VALUES(?,?,?)",
            (source.workspace_id, source.workspace_id, utc_now_ts()),
        )
        connection.execute(
            "INSERT OR IGNORE INTO projects(project_id,workspace_id,name,created_at) VALUES(?,?,?,?)",
            (source.project_id, source.workspace_id, source.project_id, utc_now_ts()),
        )
        _ensure_source(connection, source, resolved_input)
        conversation_ids = sorted({row.conversation_id for row in selected if row.conversation_id is not None})
        if len(conversation_ids) == 1:
            conversation_id = conversation_ids[0]
        elif not conversation_ids and len(entry["allowed_conversation_ids"]) == 1:
            conversation_id = entry["allowed_conversation_ids"][0]
        else:
            raise InputValidationError("raw ingestion run must target exactly one conversation")
        cursor_before = _load_cursor(connection, source.workspace_id, source.project_id, source.source_id, conversation_id)
        _write_run(connection, run_id=run_id, batch_id=batch_id, source=source,
                   input_path=resolved_input, source_sha256=source_sha_before, mode=mode, started_at=started_at)
        _write_batch(connection, run_id=run_id, batch_id=batch_id, source=source,
                     conversation_id=conversation_id, created_at=started_at)
        canonical_before = _counts(connection)
        accepted = duplicate = quarantine = failed = attachment_review = 0
        quarantine_breakdown: Counter[str] = Counter()
        max_observed = max(
            int(cursor_before["max_observed_sequence"]),
            max((row.sequence for row in rows if row.sequence is not None), default=-1),
        )
        for row in selected:
            if row.error_code is not None:
                if row.conversation_id is None:
                    row = replace(row, conversation_id=conversation_id)
                _insert_record(connection, run_id=run_id, batch_id=batch_id, source=source, row=row,
                               status="quarantined", raw_committed=False)
                quarantine += 1
                quarantine_breakdown[row.error_code] += 1
                continue
            assert row.message_id is not None and row.revision_id is not None and row.conversation_id is not None
            existing = _existing_event(connection, row, source)
            if existing:
                if existing["source_unit_hash"] != row.source_unit_hash or existing["event_type"] != row.event_type:
                    row = replace(row, error_code="revision_conflict", error_message="existing event revision differs")
                    _insert_record(connection, run_id=run_id, batch_id=batch_id, source=source, row=row,
                                   status="quarantined", raw_committed=False)
                    quarantine += 1
                    quarantine_breakdown["revision_conflict"] += 1
                else:
                    _insert_record(connection, run_id=run_id, batch_id=batch_id, source=source, row=row,
                                   status="duplicate", raw_committed=True)
                    duplicate += 1
                    if row.attachment_review:
                        attachment_review += 1
                continue
            if row.sequence is not None and row.sequence < int(cursor_before["last_contiguous_committed_sequence"]):
                existing_messages = _sequence_messages(connection, row, source)
                if row.message_id not in existing_messages:
                    row = replace(row, error_code="late_arrival", error_message="new message arrived below committed watermark")
                    _insert_record(connection, run_id=run_id, batch_id=batch_id, source=source, row=row,
                                   status="quarantined", raw_committed=False)
                    quarantine += 1
                    quarantine_breakdown["late_arrival"] += 1
                    continue
                if _sequence_has_other_message(connection, row, source):
                    row = replace(row, error_code="sequence_conflict", error_message="sequence belongs to another message")
                    _insert_record(connection, run_id=run_id, batch_id=batch_id, source=source, row=row,
                                   status="quarantined", raw_committed=False)
                    quarantine += 1
                    quarantine_breakdown["sequence_conflict"] += 1
                    continue
            if _sequence_has_other_message(connection, row, source):
                row = replace(row, error_code="sequence_conflict", error_message="sequence belongs to another message")
                _insert_record(connection, run_id=run_id, batch_id=batch_id, source=source, row=row,
                               status="quarantined", raw_committed=False)
                quarantine += 1
                quarantine_breakdown["sequence_conflict"] += 1
                continue
            try:
                _commit_event(connection, row, source, source.source_revision_id)
            except InputValidationError as exc:
                row = replace(row, error_code="source_unit_conflict", error_message=str(exc))
                _insert_record(connection, run_id=run_id, batch_id=batch_id, source=source, row=row,
                               status="quarantined", raw_committed=False)
                quarantine += 1
                quarantine_breakdown["source_unit_conflict"] += 1
                continue
            _insert_record(connection, run_id=run_id, batch_id=batch_id, source=source, row=row,
                           status="accepted", raw_committed=True)
            accepted += 1
            if row.attachment_review:
                attachment_review += 1

        cursor_after = _update_cursor(connection, source=source, conversation_id=conversation_id,
                                      max_observed=max_observed, quarantine_count=quarantine)
        canonical_after = _counts(connection)
        if canonical_before != canonical_after:
            raise RuntimeError("canonical/outbox counters changed during raw-only ingestion")
        if accepted == 0 and duplicate == 0 and quarantine > 0:
            status = "quarantined"
        elif remaining > 0 or quarantine > 0 or cursor_after["last_contiguous_committed_sequence"] < max_observed:
            status = "partial"
        else:
            status = "completed"
        summary.update({
            "status": status, "run_id": run_id, "batch_id": batch_id, "conversation_id": conversation_id,
            "processed_count": len(selected), "remaining_count": remaining,
            "accepted_count": accepted, "duplicate_count": duplicate, "quarantine_count": quarantine,
            "failed_count": failed, "attachment_review_count": attachment_review,
            "quarantine_breakdown": dict(sorted(quarantine_breakdown.items())),
            "cursor_before": {"max_observed_sequence": cursor_before["max_observed_sequence"],
                              "last_contiguous_committed_sequence": cursor_before["last_contiguous_committed_sequence"],
                              "status": cursor_before["status"]},
            "cursor_after": {"max_observed_sequence": cursor_after["max_observed_sequence"],
                             "last_contiguous_committed_sequence": cursor_after["last_contiguous_committed_sequence"],
                             "status": cursor_after["status"]},
            "canonical_before": canonical_before, "canonical_after": canonical_after, "db_write": True,
        })
        connection.execute(
            """UPDATE ingestion_batches SET status=?,accepted_count=?,duplicate_count=?,quarantine_count=?,
               failed_count=?,attachment_review_count=?,finished_at=?,summary_json=? WHERE batch_id=?""",
            (status, accepted, duplicate, quarantine, failed, attachment_review, utc_now_ts(),
             _compact_json(summary), batch_id),
        )
        connection.execute(
            """UPDATE ingestion_runs SET status=?,finished_at=?,cursor_before_json=?,cursor_after_json=?,summary_json=?
               WHERE run_id=?""",
            (status, utc_now_ts(), _compact_json(summary["cursor_before"]), _compact_json(summary["cursor_after"]),
             _compact_json(summary), run_id),
        )
    return summary
