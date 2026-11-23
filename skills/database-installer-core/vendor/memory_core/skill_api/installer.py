from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import sqlite3
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import pydantic
from pydantic import BaseModel, ConfigDict, Field

from .. import db as core_db
from ..db import SQLiteStore
from ..models import SensitivityLevel
from ..profiles import ProfileRegistry
from .backup import apply_restore as _core_apply_restore
from .backup import plan_restore as _core_plan_restore
from .backup import RestorePlan, RestoreResult
from .models import DatabaseProfile, SkillApiBlocked, SkillBackend

INSTALLER_API_VERSION = "1.4.0"
SUPPORTED_PYTHON_MIN = (3, 11)
SUPPORTED_PYTHON_MAX = (3, 13)
DEMO_CANONICAL_REL = "data/memory.sqlite3"
DEMO_PROFILE_REL = "config/connection-profile.json"
DEMO_BACKUP_REL = "backups"
DEMO_RUNTIME_REL = "runtime"
DEMO_STATE_REL = "state/install-state.json"


class CapabilitySet(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    sqlite: Literal["installed", "missing", "corrupt"] = "missing"
    fts: Literal["available", "unavailable"] = "unavailable"
    vector: Literal["disabled", "deferred", "connected", "degraded"] = "disabled"
    embedding: Literal["not_configured", "configured_unverified", "verified"] = "not_configured"


class ConnectionProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    contract_version: Literal["database-installer-profile-v1"] = "database-installer-profile-v1"
    profile_version: Literal["1.0.0"] = "1.0.0"
    installer_contract_version: Literal["database-installer-core-v1"] = "database-installer-core-v1"
    mode: Literal["demo", "real"] = "demo"
    environment: str = "demo"
    backend: Literal["sqlite", "milvus"] = "sqlite"
    workspace_root: Path
    data_dir: str = "data"
    canonical_db: str = DEMO_CANONICAL_REL
    config_dir: str = "config"
    profile_file: str = DEMO_PROFILE_REL
    backup_dir: str = DEMO_BACKUP_REL
    runtime_dir: str = DEMO_RUNTIME_REL
    state_file: str = DEMO_STATE_REL
    capabilities: CapabilitySet
    workspace_id: str
    project_ids: tuple[str, ...]
    domain_profile: str
    owner_principal_id: str | None = None
    allowed_visibilities: tuple[str, ...] = ("private", "workspace", "shared")
    max_sensitivity: SensitivityLevel = SensitivityLevel.PRIVATE
    secret_refs: tuple[str, ...] = ()
    portable: bool = True
    demo_dataset_id: str | None = None
    demo_dataset_hash: str | None = None
    manifest_version: str | None = None
    created_at: datetime


class DemoManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    manifest_version: str
    dataset_id: str
    dataset_version: str
    schema_contract: str
    seed: int
    base_timestamp_utc: int
    workspace_id: str
    project_id: str
    domain_profile: str
    profile_version: str
    owner_principal_id: str
    fixture_file: str
    fixture_sha256: str
    expected_record_hash: str
    record_count: int = Field(gt=0)
    synthetic: bool
    redacted: bool


class DemoPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    contract_version: Literal["database-installer-core-v1"] = "database-installer-core-v1"
    status: Literal["planned", "blocked"] = "planned"
    plan_id: str
    confirmation_token: str
    dataset_id: str
    workspace_root: Path
    manifest_path: Path
    fixture_path: Path
    manifest_sha256: str
    fixture_sha256: str
    dataset_hash: str
    canonical_db: Path
    profile_path: Path
    capabilities: CapabilitySet
    checks: dict[str, bool] = Field(default_factory=dict)
    blocked_reasons: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class DemoApplyResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    contract_version: Literal["database-installer-core-v1"] = "database-installer-core-v1"
    status: Literal["applied", "already_installed"]
    plan_id: str
    canonical_db: Path
    profile_path: Path
    dataset_id: str
    dataset_hash: str
    record_count: int
    capabilities: CapabilitySet


class DoctorResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    status: Literal["ok", "blocked"]
    checks: dict[str, bool]
    details: dict[str, Any]
    secret_refs: dict[str, bool] = Field(default_factory=dict)
    repair_plan: dict[str, Any] | None = None


class SecretScanResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    status: Literal["clean", "warning", "blocked"]
    blocked_reasons: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    redaction_counts: dict[str, int] = Field(default_factory=dict)


class SmokeResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    status: Literal["ok", "blocked"]
    checks: dict[str, bool]
    counts: dict[str, int]
    message: str


class StatusResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    status: Literal["ok", "warning", "blocked"]
    installation_status: Literal["installed", "partial", "missing", "blocked"]
    runtime_status: Literal["running", "stopped", "not_applicable"]
    capabilities: CapabilitySet
    checks: dict[str, bool]
    profile_path: Path
    canonical_db: Path | None = None
    warnings: tuple[str, ...] = ()


class ProfileExportResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    status: Literal["exported"] = "exported"
    output_path: Path
    portable: bool
    secret_refs: tuple[str, ...]


class InstallerBackupResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    status: Literal["created"] = "created"
    backup_path: Path
    profile_backup_path: Path
    manifest_path: Path
    canonical_sha256: str
    backup_sha256: str
    counts: dict[str, int]


class UninstallResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    status: Literal["uninstalled"] = "uninstalled"
    removed_runtime: bool
    preserved_data: bool
    preserved_profile: bool
    preserved_backups: bool


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("openai_key", re.compile(r"(?i)\bsk-[a-z0-9_-]{16,}\b")),
    ("private_key", re.compile(r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----", re.IGNORECASE)),
    ("authorization", re.compile(r"(?i)\bAuthorization\s*[:=]\s*Bearer\s+[A-Za-z0-9._~+/=-]{8,}")),
    ("bearer_token", re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}")),
    ("credentialed_uri", re.compile(r"(?i)\b(?:https?|milvus|postgres(?:ql)?|mysql)://[^\s/@:]+:[^\s/@]+@")),
    ("secret_assignment", re.compile(r"(?i)\b(?:api[_-]?key|token|password|passwd|secret[_-]?key|client[_-]?secret|access[_-]?token|signing[_-]?key)\s*[:=]\s*(?:\"[^\"\n]{8,}\"|'[^'\n]{8,}')")),
    ("unquoted_secret_assignment", re.compile(r"(?i)\b(?:api[_-]?key|token|password|passwd|secret[_-]?key|client[_-]?secret|access[_-]?token|signing[_-]?key)\s*[:=]\s*(?!os\.environ(?:\.get)?\(|os\.getenv\(|env\.|process\.env\.|\$\{|%[a-z_]|<|null\b|none\b|true\b|false\b)[a-z0-9._~+/=-]{8,}")),
)
_ORDINARY_HASH_PATTERN = re.compile(r"(?i)\b(?:[a-f0-9]{64}|[a-f0-9]{8}-[a-f0-9-]{27,36})\b")


def scan_for_secrets(text: str, *, secret_refs: tuple[str, ...] = ()) -> SecretScanResult:
    """Scan serialized output without ever returning the secret value itself."""

    counts = {"known_secret": 0, "authorization": 0, "credentialed_uri": 0, "secret_assignment": 0}
    blocked: list[str] = []
    for ref in secret_refs:
        value = os.environ.get(ref, "")
        if len(value) >= 8 and value in text:
            counts["known_secret"] += 1
            blocked.append("known_secret_value")
    for name, pattern in _SECRET_PATTERNS:
        matches = len(pattern.findall(text))
        if matches:
            counts[name] = counts.get(name, 0) + matches
            blocked.append(name)
    hash_matches = len(_ORDINARY_HASH_PATTERN.findall(text))
    warnings = ["ordinary_hash_or_uuid"] if hash_matches else []
    if blocked:
        status: Literal["clean", "warning", "blocked"] = "blocked"
    elif warnings:
        status = "warning"
    else:
        status = "clean"
    return SecretScanResult(
        status=status,
        blocked_reasons=tuple(dict.fromkeys(blocked)),
        warnings=tuple(warnings),
        redaction_counts={key: value for key, value in counts.items() if value},
    )


def _record_hash(records: list[dict[str, Any]]) -> str:
    ordered = sorted(records, key=lambda item: str(item["memory_id"]))
    payload = "\n".join(_canonical_json(item) for item in ordered) + "\n"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _safe_relative(value: str, root: Path) -> Path:
    candidate = Path(value)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise SkillApiBlocked("managed path must be relative")
    resolved = (root / candidate).resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise SkillApiBlocked("managed path escapes workspace_root")
    return resolved


def _workspace_paths(root: str | Path) -> dict[str, Path]:
    workspace = Path(root).resolve()
    return {
        "workspace_root": workspace,
        "data": _safe_relative("data", workspace),
        "canonical_db": _safe_relative(DEMO_CANONICAL_REL, workspace),
        "config": _safe_relative("config", workspace),
        "profile": _safe_relative(DEMO_PROFILE_REL, workspace),
        "backups": _safe_relative(DEMO_BACKUP_REL, workspace),
        "runtime": _safe_relative(DEMO_RUNTIME_REL, workspace),
        "state": _safe_relative(DEMO_STATE_REL, workspace),
    }
def _load_demo(manifest_path: str | Path) -> tuple[DemoManifest, Path, Path, list[dict[str, Any]], str, str]:
    path = Path(manifest_path).resolve()
    if not path.is_file():
        raise SkillApiBlocked("demo manifest does not exist")
    manifest = DemoManifest.model_validate_json(path.read_text(encoding="utf-8"))
    if not manifest.synthetic or not manifest.redacted:
        raise SkillApiBlocked("demo fixture must be synthetic and redacted")
    fixture_rel = Path(manifest.fixture_file)
    if fixture_rel.is_absolute() or ".." in fixture_rel.parts:
        raise SkillApiBlocked("demo fixture path must be relative")
    fixture = (path.parent / fixture_rel).resolve()
    if not fixture.is_relative_to(path.parent.resolve()) or not fixture.is_file():
        raise SkillApiBlocked("demo fixture is missing or outside manifest directory")
    fixture_hash = _sha256_file(fixture)
    if fixture_hash != manifest.fixture_sha256:
        raise SkillApiBlocked("demo fixture hash mismatch")
    records: list[dict[str, Any]] = []
    for line in fixture.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        required = {"memory_id", "source_id", "source_unit_id", "content", "memory_type"}
        if not required.issubset(record):
            raise SkillApiBlocked("demo fixture record is missing required fields")
        records.append(record)
    if len(records) != manifest.record_count:
        raise SkillApiBlocked("demo record count mismatch")
    dataset_hash = _record_hash(records)
    if dataset_hash != manifest.expected_record_hash:
        raise SkillApiBlocked("demo record hash mismatch")
    return manifest, path, fixture, records, _sha256_file(path), fixture_hash


def _plan_id(manifest_hash: str, dataset_hash: str, workspace_root: Path) -> str:
    payload = {
        "manifest_sha256": manifest_hash,
        "dataset_hash": dataset_hash,
        "workspace_root": str(workspace_root.resolve()),
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _profile_capabilities() -> CapabilitySet:
    return CapabilitySet(sqlite="installed", fts="available", vector="disabled", embedding="not_configured")


def _make_profile(manifest: DemoManifest, workspace_root: Path) -> ConnectionProfile:
    return ConnectionProfile(
        workspace_root=workspace_root.resolve(),
        capabilities=_profile_capabilities(),
        workspace_id=manifest.workspace_id,
        project_ids=(manifest.project_id,),
        domain_profile=manifest.domain_profile,
        owner_principal_id=manifest.owner_principal_id,
        max_sensitivity=SensitivityLevel.INTERNAL,
        demo_dataset_id=manifest.dataset_id,
        demo_dataset_hash=manifest.expected_record_hash,
        manifest_version=manifest.manifest_version,
        created_at=datetime.fromtimestamp(manifest.base_timestamp_utc, timezone.utc),
    )


def _read_install_state(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(text, encoding="utf-8", newline="\n")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def doctor(workspace_root: str | Path, *, secret_refs: tuple[str, ...] = (), repair_plan: dict[str, Any] | None = None) -> DoctorResult:
    workspace = Path(workspace_root).resolve()
    pydantic_version = str(getattr(pydantic, "VERSION", getattr(pydantic, "__version__", "")))
    try:
        pydantic_major = int(pydantic_version.split(".", 1)[0])
    except ValueError:
        pydantic_major = 0
    checks: dict[str, bool] = {
        "os_windows": platform.system().casefold() == "windows",
        "python_supported": SUPPORTED_PYTHON_MIN <= sys.version_info[:2] <= SUPPORTED_PYTHON_MAX,
        "pydantic_v2": pydantic_major == 2,
    }
    try:
        probe = sqlite3.connect(":memory:")
        try:
            probe.execute("CREATE VIRTUAL TABLE fts_probe USING fts5(content, tokenize='trigram')")
            probe.execute("INSERT INTO fts_probe(content) VALUES ('demo probe')")
            checks["sqlite_available"] = True
            checks["fts5_trigram"] = probe.execute(
                "SELECT count(*) FROM fts_probe WHERE fts_probe MATCH 'probe'"
            ).fetchone()[0] == 1
        finally:
            probe.close()
    except sqlite3.Error:
        checks["sqlite_available"] = False
        checks["fts5_trigram"] = False
    existing = workspace
    while not existing.exists() and existing != existing.parent:
        existing = existing.parent
    checks["workspace_parent_writable"] = existing.is_dir() and os.access(existing, os.W_OK)
    secret_map = {name: name in os.environ for name in secret_refs}
    return DoctorResult(
        status="ok" if all(checks.values()) else "blocked",
        checks=checks,
        details={
            "platform": platform.system(),
            "platform_release": platform.release(),
            "python_version": platform.python_version(),
            "sqlite_version": sqlite3.sqlite_version,
            "pydantic_version": pydantic_version,
            "workspace_root": str(workspace),
            "secret_ref_names": list(secret_refs),
        },
        secret_refs=secret_map,
        repair_plan=repair_plan,
    )
def _seed_demo_database(db_path: Path, manifest: DemoManifest, records: list[dict[str, Any]]) -> None:
    store = SQLiteStore(db_path)
    store.migrate()
    store.ensure_workspace_project(
        workspace_id=manifest.workspace_id,
        workspace_name="Synthetic Demo",
        project_id=manifest.project_id,
        project_name="Synthetic Demo Project",
    )
    profiles = ProfileRegistry()
    store.register_profiles(profiles)
    profile = profiles.get(manifest.domain_profile, manifest.profile_version)
    base = int(manifest.base_timestamp_utc)
    with store.transaction() as connection:
        for index, record in enumerate(sorted(records, key=lambda item: item["memory_id"])):
            now = base + index
            source_id = str(record["source_id"])
            unit_id = str(record["source_unit_id"])
            content = str(record["content"])
            subject = str(record.get("subject") or "")
            normalized_subject = profile.normalize_subject(subject, None)
            unit_hash = hashlib.sha256(f"{source_id}|{unit_id}|{content}".encode("utf-8")).hexdigest()
            content_hash = hashlib.sha256(
                f"v1|{manifest.domain_profile}|{normalized_subject}|{profile.normalize_content(content)}".encode("utf-8")
            ).hexdigest()
            connection.execute(
                """INSERT INTO sources(source_id,workspace_id,project_id,visibility,owner_principal_id,
                   source_path,source_sha256,source_type,sensitivity,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (source_id, manifest.workspace_id, manifest.project_id, "workspace", None,
                 f"demo://{source_id}", hashlib.sha256(content.encode("utf-8")).hexdigest(),
                 "synthetic_demo", "internal", now),
            )
            connection.execute(
                """INSERT INTO source_units(source_unit_id,source_id,parent_unit_id,scope_type,scope_ref,
                   source_unit_hash,text_content,metadata_json,created_at,project_id) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (unit_id, source_id, None, "synthetic_demo", str(record["memory_id"]), unit_hash, content, "{}", now, manifest.project_id),
            )
            connection.execute(
                """INSERT INTO memory_items(memory_id,workspace_id,project_id,task_id,conversation_id,
                   owner_principal_id,domain_profile,profile_version,memory_type,subtype,subject,
                   normalized_subject,entity_type,entity_id,content,content_hash,content_hash_version,
                   dedup_policy,status,importance,sensitivity,visibility,effective_from,effective_to,
                   source_id,source_unit_id,source_scope_json,confidence,supersedes_id,version,
                   domain_payload_json,idempotency_key,deleted_at,delete_reason,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (record["memory_id"], manifest.workspace_id, manifest.project_id, None, None,
                 manifest.owner_principal_id, manifest.domain_profile, manifest.profile_version,
                 record["memory_type"], record.get("subtype"), subject or None, normalized_subject, None, None,
                 content, content_hash, "v1", "content_only", "pending_review", record.get("importance", "medium"),
                 record.get("sensitivity", "internal"), record.get("visibility", "workspace"), None, None,
                 source_id, unit_id, _canonical_json(record.get("source_scope") or {}),
                 float(record.get("confidence", 0.99)), None, 1,
                 _canonical_json(record.get("domain_payload") or {}), f"demo:{record['memory_id']}",
                 None, None, now, now),
            )
            connection.execute(
                "INSERT INTO memory_evidence(evidence_id,memory_id,source_unit_id,quote,locator_json,evidence_status,created_at) VALUES(?,?,?,?,?,?,?)",
                (f"demo-evidence-{index + 1}", record["memory_id"], unit_id, content, "{}", "verified", now),
            )
            connection.execute("UPDATE memory_items SET status='active',updated_at=? WHERE memory_id=?", (now, record["memory_id"]))
            connection.execute(
                """INSERT INTO memory_fts(memory_id,workspace_id,project_id,task_id,conversation_id,
                   owner_principal_id,domain_profile,memory_type,status,sensitivity,visibility,content)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (record["memory_id"], manifest.workspace_id, manifest.project_id, "", "",
                 manifest.owner_principal_id, manifest.domain_profile, record["memory_type"], "active",
                 record.get("sensitivity", "internal"), record.get("visibility", "workspace"), content),
            )
            connection.execute(
                "INSERT INTO memory_fts_sync_state(memory_id,item_version,deleted,updated_at) VALUES(?,?,?,?)",
                (record["memory_id"], 1, 0, now),
            )
def demo_plan(*, workspace_root: str | Path, manifest_path: str | Path) -> DemoPlan:
    manifest, path, fixture, records, manifest_sha, fixture_sha = _load_demo(manifest_path)
    paths = _workspace_paths(workspace_root)
    checks = {
        "manifest_valid": True,
        "fixture_valid": True,
        "record_count_valid": len(records) == manifest.record_count,
        "synthetic_fixture": manifest.synthetic and manifest.redacted,
        "profile_inside_root": paths["profile"].is_relative_to(paths["workspace_root"]),
    }
    warnings: list[str] = []
    state = _read_install_state(paths["state"])
    if paths["canonical_db"].exists():
        if state and state.get("dataset_hash") == manifest.expected_record_hash:
            warnings.append("demo target is already installed; apply will be idempotent")
            checks["target_absent_or_known_install"] = True
        else:
            checks["target_absent_or_known_install"] = False
    else:
        checks["target_absent_or_known_install"] = True
    blocked = [name for name, passed in checks.items() if not passed]
    plan_id = _plan_id(manifest_sha, manifest.expected_record_hash, paths["workspace_root"])
    return DemoPlan(
        status="blocked" if blocked else "planned",
        plan_id=plan_id,
        confirmation_token=f"demo:{manifest.dataset_id}:{plan_id[:12]}",
        dataset_id=manifest.dataset_id,
        workspace_root=paths["workspace_root"],
        manifest_path=path,
        fixture_path=fixture,
        manifest_sha256=manifest_sha,
        fixture_sha256=fixture_sha,
        dataset_hash=manifest.expected_record_hash,
        canonical_db=paths["canonical_db"],
        profile_path=paths["profile"],
        capabilities=_profile_capabilities(),
        checks=checks,
        blocked_reasons=tuple(dict.fromkeys(blocked)),
        warnings=tuple(warnings),
    )


def demo_apply(plan: DemoPlan, *, confirmation_token: str) -> DemoApplyResult:
    if plan.status != "planned":
        raise SkillApiBlocked("demo plan is blocked: " + ", ".join(plan.blocked_reasons))
    if confirmation_token != plan.confirmation_token:
        raise SkillApiBlocked("demo confirmation token does not match plan")
    manifest, _, _, records, manifest_sha, fixture_sha = _load_demo(plan.manifest_path)
    if manifest_sha != plan.manifest_sha256 or fixture_sha != plan.fixture_sha256 or manifest.expected_record_hash != plan.dataset_hash:
        raise SkillApiBlocked("demo manifest or fixture changed after planning")
    state_path = plan.workspace_root / DEMO_STATE_REL
    if plan.canonical_db.exists():
        state = _read_install_state(state_path)
        if state and state.get("dataset_hash") == plan.dataset_hash and state.get("manifest_sha256") == plan.manifest_sha256:
            profile = _make_profile(manifest, plan.workspace_root)
            return DemoApplyResult(
                status="already_installed", plan_id=plan.plan_id,
                canonical_db=plan.canonical_db, profile_path=plan.profile_path,
                dataset_id=manifest.dataset_id, dataset_hash=plan.dataset_hash,
                record_count=manifest.record_count, capabilities=profile.capabilities,
            )
        raise SkillApiBlocked("demo target already exists with different or unknown contents")
    plan.canonical_db.parent.mkdir(parents=True, exist_ok=True)
    plan.profile_path.parent.mkdir(parents=True, exist_ok=True)
    (plan.workspace_root / "runtime").mkdir(parents=True, exist_ok=True)
    (plan.workspace_root / "backups").mkdir(parents=True, exist_ok=True)
    try:
        _seed_demo_database(plan.canonical_db, manifest, records)
        connection = sqlite3.connect(f"file:{plan.canonical_db.as_posix()}?mode=ro", uri=True)
        try:
            active = connection.execute("SELECT count(*) FROM memory_items WHERE status='active' AND deleted_at IS NULL").fetchone()[0]
            fts = connection.execute("SELECT count(*) FROM memory_fts").fetchone()[0]
        finally:
            connection.close()
        if int(active) != manifest.record_count or int(fts) != manifest.record_count:
            raise SkillApiBlocked("demo post-install counts do not match manifest")
        profile = _make_profile(manifest, plan.workspace_root)
        _write_text_atomic(plan.profile_path, profile.model_dump_json(indent=2) + "\n")
        _write_text_atomic(
            state_path,
            json.dumps({
                "installer_contract_version": "database-installer-core-v1",
                "dataset_id": manifest.dataset_id,
                "dataset_hash": plan.dataset_hash,
                "manifest_sha256": plan.manifest_sha256,
                "fixture_sha256": plan.fixture_sha256,
                "record_count": manifest.record_count,
                "installed_at": datetime.now(timezone.utc).isoformat(),
            }, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
    except Exception:
        if plan.canonical_db.exists():
            plan.canonical_db.unlink()
        raise
    return DemoApplyResult(
        status="applied", plan_id=plan.plan_id,
        canonical_db=plan.canonical_db, profile_path=plan.profile_path,
        dataset_id=manifest.dataset_id, dataset_hash=plan.dataset_hash,
        record_count=manifest.record_count, capabilities=profile.capabilities,
    )


def load_connection_profile(path: str | Path) -> ConnectionProfile:
    profile_path = Path(path).resolve()
    if not profile_path.is_file():
        raise SkillApiBlocked("connection profile does not exist")
    return ConnectionProfile.model_validate_json(profile_path.read_text(encoding="utf-8"))
def smoke(profile_path: str | Path) -> SmokeResult:
    profile = load_connection_profile(profile_path)
    db = (profile.workspace_root / profile.canonical_db).resolve()
    if not db.is_file():
        return SmokeResult(status="blocked", checks={"canonical_exists": False}, counts={}, message="canonical database is missing")
    probe = f"smoke-{uuid.uuid4().hex[:12]}"
    source_id = f"smoke-src-{probe}"
    unit_id = f"smoke-unit-{probe}"
    memory_id = f"mem-{probe}"
    idempotency_key = f"smoke-idem-{probe}"
    checks: dict[str, bool] = {}
    connection = sqlite3.connect(db)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("BEGIN IMMEDIATE")
        now = int(datetime.now(timezone.utc).timestamp())
        content = "synthetic smoke probe"
        unit_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        connection.execute(
            "INSERT INTO sources(source_id,workspace_id,project_id,visibility,owner_principal_id,source_path,source_sha256,source_type,sensitivity,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (source_id, profile.workspace_id, profile.project_ids[0], "workspace", None, "memory://smoke", unit_hash, "smoke", "internal", now),
        )
        connection.execute(
            "INSERT INTO source_units(source_unit_id,source_id,parent_unit_id,scope_type,scope_ref,source_unit_hash,text_content,metadata_json,created_at,project_id) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (unit_id, source_id, None, "smoke", probe, unit_hash, content, "{}", now, profile.project_ids[0]),
        )
        insert_memory = """INSERT INTO memory_items(
            memory_id,workspace_id,project_id,task_id,conversation_id,owner_principal_id,
            domain_profile,profile_version,memory_type,subtype,subject,normalized_subject,entity_type,entity_id,
            content,content_hash,content_hash_version,dedup_policy,status,importance,sensitivity,visibility,
            effective_from,effective_to,source_id,source_unit_id,source_scope_json,confidence,supersedes_id,
            version,domain_payload_json,idempotency_key,deleted_at,delete_reason,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"""
        values = (
            memory_id, profile.workspace_id, profile.project_ids[0], None, None,
            profile.owner_principal_id or "smoke", profile.domain_profile, "1.0.0",
            "fact", None, "smoke", "smoke", None, None, content, unit_hash, "v1", "content_only",
            "pending_review", "medium", "internal", "workspace", None, None, source_id, unit_id,
            "{}", 0.99, None, 1, "{}", idempotency_key, None, None, now, now,
        )
        connection.execute(insert_memory, values)
        connection.execute(
            "INSERT INTO memory_evidence(evidence_id,memory_id,source_unit_id,quote,locator_json,evidence_status,created_at) VALUES(?,?,?,?,?,?,?)",
            (f"ev-{probe}", memory_id, unit_id, content, "{}", "verified", now),
        )
        connection.execute("UPDATE memory_items SET status='active' WHERE memory_id=?", (memory_id,))
        connection.execute(
            "INSERT INTO memory_fts(memory_id,workspace_id,project_id,task_id,conversation_id,owner_principal_id,domain_profile,memory_type,status,sensitivity,visibility,content) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (memory_id, profile.workspace_id, profile.project_ids[0], "", "", profile.owner_principal_id or "smoke", profile.domain_profile, "fact", "active", "internal", "workspace", content),
        )
        connection.execute("INSERT INTO memory_fts_sync_state(memory_id,item_version,deleted,updated_at) VALUES(?,?,?,?)", (memory_id, 1, 0, now))
        checks["write"] = True
        duplicate_values = list(values)
        duplicate_values[0] = f"mem-dup-{probe}"
        try:
            connection.execute(insert_memory, tuple(duplicate_values))
            duplicate_blocked = False
        except sqlite3.IntegrityError:
            duplicate_blocked = True
        checks["idempotency"] = duplicate_blocked
        checks["read"] = connection.execute("SELECT content FROM memory_items WHERE memory_id=?", (memory_id,)).fetchone()[0] == content
        checks["fts"] = connection.execute("SELECT COUNT(*) FROM memory_fts WHERE memory_id=?", (memory_id,)).fetchone()[0] == 1
    finally:
        connection.rollback()
        connection.close()
    read_connection = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        checks["cleanup"] = read_connection.execute("SELECT COUNT(*) FROM memory_items WHERE memory_id=?", (memory_id,)).fetchone()[0] == 0
    finally:
        read_connection.close()
    return SmokeResult(
        status="ok" if all(checks.values()) else "blocked",
        checks=checks,
        counts={"probe_memories": 0},
        message="temporary probe rolled back",
    )
def status(profile_path: str | Path) -> StatusResult:
    profile = load_connection_profile(profile_path)
    db = (profile.workspace_root / profile.canonical_db).resolve()
    checks: dict[str, bool] = {"profile_exists": True, "canonical_exists": db.is_file()}
    if not db.is_file():
        return StatusResult(
            status="blocked", installation_status="missing", runtime_status="not_applicable",
            capabilities=CapabilitySet(sqlite="missing", fts="unavailable", vector=profile.capabilities.vector, embedding=profile.capabilities.embedding),
            checks=checks, profile_path=Path(profile_path).resolve(), canonical_db=db,
        )
    connection = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        checks["integrity"] = connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        checks["schema_migrations"] = connection.execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND name='schema_migrations'").fetchone()[0] == 1
        checks["fts"] = connection.execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND name='memory_fts'").fetchone()[0] == 1
    finally:
        connection.close()
    installed = checks["integrity"] and checks["schema_migrations"] and checks["fts"]
    capabilities = CapabilitySet(
        sqlite="installed" if checks["integrity"] else "corrupt",
        fts="available" if checks["fts"] else "unavailable",
        vector=profile.capabilities.vector,
        embedding=profile.capabilities.embedding,
    )
    return StatusResult(
        status="ok" if installed else "warning",
        installation_status="installed" if installed else "partial",
        runtime_status="not_applicable",
        capabilities=capabilities,
        checks=checks,
        profile_path=Path(profile_path).resolve(),
        canonical_db=db,
        warnings=() if installed else ("installation is partial",),
    )


def export_profile(profile_path: str | Path, output_path: str | Path) -> ProfileExportResult:
    profile = load_connection_profile(profile_path)
    serialized = profile.model_dump_json(indent=2) + "\n"
    scan = scan_for_secrets(serialized, secret_refs=profile.secret_refs)
    if scan.status == "blocked":
        raise SkillApiBlocked("profile export blocked by secret scan: " + ", ".join(scan.blocked_reasons))
    output = Path(output_path).resolve()
    _write_text_atomic(output, serialized)
    return ProfileExportResult(output_path=output, portable=profile.portable, secret_refs=profile.secret_refs)


def installer_backup(profile_path: str | Path, backup_dir: str | Path | None = None) -> InstallerBackupResult:
    profile = load_connection_profile(profile_path)
    canonical = (profile.workspace_root / profile.canonical_db).resolve()
    if not canonical.is_file():
        raise SkillApiBlocked("canonical database does not exist")
    directory = (profile.workspace_root / profile.backup_dir).resolve() if backup_dir is None else Path(backup_dir).resolve()
    if directory == canonical.parent:
        raise SkillApiBlocked("backup directory must not be the canonical directory")
    directory.mkdir(parents=True, exist_ok=True)
    backup_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = directory / f"memory_{backup_id}.sqlite3"
    profile_backup_path = directory / f"memory_{backup_id}.profile.json"
    manifest_path = directory / f"memory_{backup_id}.manifest.json"
    if backup_path.exists() or profile_backup_path.exists() or manifest_path.exists():
        backup_id = f"{backup_id}-{uuid.uuid4().hex[:8]}"
        backup_path = directory / f"memory_{backup_id}.sqlite3"
        profile_backup_path = directory / f"memory_{backup_id}.profile.json"
        manifest_path = directory / f"memory_{backup_id}.manifest.json"
    shutil.copy2(Path(profile_path).resolve(), profile_backup_path)
    source = sqlite3.connect(f"file:{canonical.as_posix()}?mode=ro", uri=True)
    target = sqlite3.connect(backup_path)
    try:
        source.backup(target, pages=1000)
        target.commit()
    finally:
        target.close()
        source.close()
    check = sqlite3.connect(f"file:{backup_path.as_posix()}?mode=ro", uri=True)
    try:
        integrity = check.execute("PRAGMA integrity_check").fetchone()[0]
        active = check.execute("SELECT count(*) FROM memory_items WHERE status='active' AND deleted_at IS NULL").fetchone()[0]
        fts = check.execute("SELECT count(*) FROM memory_fts").fetchone()[0]
    finally:
        check.close()
    if integrity != "ok":
        raise SkillApiBlocked("backup integrity check failed")
    manifest = {
        "contract_version": "database-installer-backup-v1",
        "backup_path": str(backup_path),
        "canonical_sha256": _sha256_file(canonical),
        "backup_sha256": _sha256_file(backup_path),
        "profile_backup_path": str(profile_backup_path),
        "profile_sha256": _sha256_file(profile_backup_path),
        "counts": {"active": int(active), "fts": int(fts)},
    }
    _write_text_atomic(manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return InstallerBackupResult(
        backup_path=backup_path,
        profile_backup_path=profile_backup_path,
        manifest_path=manifest_path,
        canonical_sha256=manifest["canonical_sha256"],
        backup_sha256=manifest["backup_sha256"],
        counts=manifest["counts"],
    )


def uninstall_runtime(profile_path: str | Path) -> UninstallResult:
    profile = load_connection_profile(profile_path)
    runtime = (profile.workspace_root / profile.runtime_dir).resolve()
    removed = False
    if runtime.exists():
        if not runtime.is_relative_to(profile.workspace_root.resolve()):
            raise SkillApiBlocked("runtime directory escapes workspace_root")
        shutil.rmtree(runtime)
        removed = True
    return UninstallResult(
        removed_runtime=removed,
        preserved_data=(profile.workspace_root / profile.canonical_db).exists(),
        preserved_profile=Path(profile_path).resolve().exists(),
        preserved_backups=(profile.workspace_root / profile.backup_dir).exists(),
    )
class UpgradePlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    contract_version: Literal["database-installer-upgrade-v1"] = "database-installer-upgrade-v1"
    status: Literal["planned", "blocked"] = "planned"
    plan_id: str
    confirmation_token: str
    profile_path: Path
    canonical_db: Path
    current_sha256: str
    current_schema_head: str | None = None
    target_schema_head: str | None = None
    applied_migrations: tuple[str, ...] = ()
    pending_migrations: tuple[str, ...] = ()
    up_to_date: bool = False
    checks: dict[str, bool] = Field(default_factory=dict)
    blocked_reasons: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class UpgradeResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    status: Literal["applied", "already_current"]
    plan_id: str
    canonical_db: Path
    current_schema_head: str | None = None
    target_schema_head: str | None = None
    pre_upgrade_backup: Path | None = None
    rollback_db: Path | None = None
    checks: dict[str, bool] = Field(default_factory=dict)


def _connection_database_profile(profile: ConnectionProfile) -> DatabaseProfile:
    canonical = (profile.workspace_root / profile.canonical_db).resolve()
    canonical_hash = _sha256_file(canonical) if canonical.is_file() else "missing"
    return DatabaseProfile(
        environment=profile.environment,
        backend=SkillBackend.SQLITE,
        capabilities=("query", "health", "backup", "restore", "migrate"),
        workspace_id=profile.workspace_id,
        project_ids=profile.project_ids,
        domain_profile=profile.domain_profile,
        owner_principal_id=profile.owner_principal_id,
        allowed_visibilities=profile.allowed_visibilities,
        max_sensitivity=profile.max_sensitivity,
        canonical_db=canonical,
        canonical_sha256=canonical_hash,
        plan_id=hashlib.sha256(str(canonical).encode("utf-8")).hexdigest()[:16],
        created_at=profile.created_at,
    )


def restore_plan(profile_path: str | Path, *, backup_path: str | Path) -> RestorePlan:
    profile = load_connection_profile(profile_path)
    database_profile = _connection_database_profile(profile)
    return _core_plan_restore(database_profile, backup_path=backup_path)


def restore_apply(
    profile_path: str | Path,
    plan: RestorePlan,
    *,
    confirmation_token: str,
    backup_dir: str | Path | None = None,
) -> RestoreResult:
    profile = load_connection_profile(profile_path)
    database_profile = _connection_database_profile(profile)
    target_dir = (
        (profile.workspace_root / profile.backup_dir).resolve()
        if backup_dir is None
        else Path(backup_dir).resolve()
    )
    pre_restore_dir = target_dir / "pre-restore"
    result = _core_apply_restore(
        database_profile,
        plan,
        confirmation_token=confirmation_token,
        pre_backup_dir=pre_restore_dir,
    )
    state_path = profile.workspace_root / profile.state_file
    state = _read_install_state(state_path) or {}
    state.update({
        "last_restore_plan_id": plan.plan_id,
        "last_restore_at": datetime.now(timezone.utc).isoformat(),
        "last_restore_backup": str(plan.source_backup),
    })
    _write_text_atomic(state_path, json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return result


def _migration_scripts() -> dict[str, str]:
    return {
        path.stem: hashlib.sha256(path.read_text(encoding="utf-8").encode("utf-8")).hexdigest()
        for path in sorted(core_db.MIGRATIONS_DIR.glob("*.sql"))
    }


def _inspect_migrations(path: Path, expected: dict[str, str]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "exists": path.is_file(),
        "readable": False,
        "integrity_ok": False,
        "foreign_keys_ok": False,
        "schema_migrations": False,
        "memory_items": False,
        "memory_fts": False,
        "no_unknown_migrations": False,
        "checksums_match": False,
        "applied": (),
        "pending": tuple(sorted(expected)),
        "schema_head": None,
        "target_head": max(expected) if expected else None,
    }
    if not path.is_file():
        return result
    try:
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            result.update({
                "readable": True,
                "integrity_ok": connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok",
                "foreign_keys_ok": len(connection.execute("PRAGMA foreign_key_check").fetchall()) == 0,
                "schema_migrations": "schema_migrations" in tables,
                "memory_items": "memory_items" in tables,
                "memory_fts": "memory_fts" in tables,
            })
            if "schema_migrations" in tables:
                applied_map = {
                    str(row["migration_id"]): str(row["checksum"])
                    for row in connection.execute("SELECT migration_id,checksum FROM schema_migrations")
                }
                applied = tuple(sorted(applied_map))
                unknown = set(applied_map) - set(expected)
                mismatch = {name for name in set(applied_map) & set(expected) if applied_map[name] != expected[name]}
                result.update({
                    "applied": applied,
                    "pending": tuple(sorted(set(expected) - set(applied_map))),
                    "schema_head": applied[-1] if applied else None,
                    "no_unknown_migrations": not unknown,
                    "checksums_match": not mismatch,
                })
        finally:
            connection.close()
    except sqlite3.Error:
        return result
    return result


def _verify_sqlite_database(path: Path, *, expected_migrations: tuple[str, ...] = ()) -> dict[str, bool]:
    expected = _migration_scripts()
    inspection = _inspect_migrations(path, expected)
    checks = {
        "database_exists": bool(inspection["exists"]),
        "sqlite_readable": bool(inspection["readable"]),
        "integrity_ok": bool(inspection["integrity_ok"]),
        "foreign_keys_ok": bool(inspection["foreign_keys_ok"]),
        "schema_migrations": bool(inspection["schema_migrations"]),
        "memory_items": bool(inspection["memory_items"]),
        "memory_fts": bool(inspection["memory_fts"]),
        "no_unknown_migrations": bool(inspection["no_unknown_migrations"]),
        "checksums_match": bool(inspection["checksums_match"]),
    }
    if expected_migrations:
        applied = set(inspection["applied"])
        checks["expected_migrations_applied"] = set(expected_migrations).issubset(applied)
        checks["no_pending_migrations"] = not bool(inspection["pending"])
    return checks


def _verify_upgrade_database(path: Path, *, expected_migrations: tuple[str, ...]) -> dict[str, bool]:
    return _verify_sqlite_database(path, expected_migrations=expected_migrations)


def _online_backup_to(source: Path, target: Path) -> None:
    if target.exists():
        raise SkillApiBlocked("staging target already exists")
    target.parent.mkdir(parents=True, exist_ok=True)
    source_connection = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
    target_connection = sqlite3.connect(target)
    try:
        source_connection.backup(target_connection, pages=1000)
        target_connection.commit()
    finally:
        target_connection.close()
        source_connection.close()


def _checkpoint_database(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.commit()
    finally:
        connection.close()


def _move_database_sidecars(source: Path, target: Path) -> None:
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(source) + suffix)
        if sidecar.exists():
            os.replace(sidecar, Path(str(target) + suffix))


def _remove_database_sidecars(path: Path) -> None:
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(path) + suffix)
        if sidecar.exists():
            sidecar.unlink()


def upgrade_plan(profile_path: str | Path) -> UpgradePlan:
    profile = load_connection_profile(profile_path)
    canonical = (profile.workspace_root / profile.canonical_db).resolve()
    expected = _migration_scripts()
    inspection = _inspect_migrations(canonical, expected)
    pending = tuple(inspection["pending"])
    checks = {
        "canonical_exists": bool(inspection["exists"]),
        "sqlite_readable": bool(inspection["readable"]),
        "integrity_ok": bool(inspection["integrity_ok"]),
        "foreign_keys_ok": bool(inspection["foreign_keys_ok"]),
        "schema_migrations": bool(inspection["schema_migrations"]),
        "memory_items": bool(inspection["memory_items"]),
        "memory_fts": bool(inspection["memory_fts"]),
        "no_unknown_migrations": bool(inspection["no_unknown_migrations"]),
        "checksums_match": bool(inspection["checksums_match"]),
        "migration_files_present": bool(expected),
    }
    blocked = [name for name, passed in checks.items() if not passed]
    current_hash = _sha256_file(canonical) if canonical.is_file() else ""
    warnings: list[str] = []
    if not pending and not blocked:
        warnings.append("database is already at the current schema head")
    material = {
        "profile_path": str(Path(profile_path).resolve()),
        "canonical_db": str(canonical),
        "current_sha256": current_hash,
        "current_schema_head": inspection["schema_head"],
        "target_schema_head": inspection["target_head"],
        "pending_migrations": pending,
    }
    plan_id = hashlib.sha256(_canonical_json(material).encode("utf-8")).hexdigest()
    return UpgradePlan(
        status="blocked" if blocked else "planned",
        plan_id=plan_id,
        confirmation_token=f"upgrade:{plan_id[:12]}",
        profile_path=Path(profile_path).resolve(),
        canonical_db=canonical,
        current_sha256=current_hash,
        current_schema_head=inspection["schema_head"],
        target_schema_head=inspection["target_head"],
        applied_migrations=tuple(inspection["applied"]),
        pending_migrations=pending,
        up_to_date=not pending,
        checks=checks,
        blocked_reasons=tuple(dict.fromkeys(blocked)),
        warnings=tuple(warnings),
    )


def upgrade_apply(
    profile_path: str | Path,
    plan: UpgradePlan,
    *,
    confirmation_token: str,
    backup_dir: str | Path | None = None,
) -> UpgradeResult:
    profile = load_connection_profile(profile_path)
    if plan.status != "planned":
        raise SkillApiBlocked("upgrade plan is blocked: " + ", ".join(plan.blocked_reasons))
    if confirmation_token != plan.confirmation_token:
        raise SkillApiBlocked("upgrade confirmation token does not match plan")
    canonical = (profile.workspace_root / profile.canonical_db).resolve()
    if canonical != plan.canonical_db.resolve() or not canonical.is_file():
        raise SkillApiBlocked("upgrade target does not match the attached canonical")
    if _sha256_file(canonical) != plan.current_sha256:
        raise SkillApiBlocked("canonical database changed after upgrade planning")
    expected = _migration_scripts()
    inspection = _inspect_migrations(canonical, expected)
    if tuple(inspection["pending"]) != plan.pending_migrations:
        raise SkillApiBlocked("migration set changed after upgrade planning")
    if plan.up_to_date:
        return UpgradeResult(
            status="already_current",
            plan_id=plan.plan_id,
            canonical_db=canonical,
            current_schema_head=inspection["schema_head"],
            target_schema_head=inspection["target_head"],
            checks={"already_current": True},
        )

    backup_root = (
        (profile.workspace_root / profile.backup_dir).resolve()
        if backup_dir is None
        else Path(backup_dir).resolve()
    )
    pre_backup = installer_backup(profile_path, backup_root / "pre-upgrade")
    stage = canonical.with_name(f".{canonical.name}.upgrade-{uuid.uuid4().hex}.tmp")
    rollback = canonical.with_name(f"{canonical.name}.pre-upgrade-{plan.plan_id[:8]}-{uuid.uuid4().hex}.sqlite3")
    replaced = False
    try:
        _online_backup_to(canonical, stage)
        initial_checks = _verify_sqlite_database(stage)
        if not all(initial_checks.values()):
            raise SkillApiBlocked("upgrade staging database failed integrity verification")
        SQLiteStore(stage).migrate()
        _checkpoint_database(stage)
        staged_checks = _verify_upgrade_database(
            stage, expected_migrations=tuple(expected)
        )
        if not all(staged_checks.values()):
            raise SkillApiBlocked("upgraded staging database verification failed")
        if _sha256_file(canonical) != plan.current_sha256:
            raise SkillApiBlocked("canonical changed during upgrade staging")
        os.replace(canonical, rollback)
        _move_database_sidecars(canonical, rollback)
        os.replace(stage, canonical)
        replaced = True
        final_checks = _verify_upgrade_database(
            canonical, expected_migrations=tuple(expected)
        )
        if not all(final_checks.values()):
            raise SkillApiBlocked("upgraded canonical verification failed")
    except Exception as exc:
        if replaced and canonical.exists():
            canonical.unlink()
        _remove_database_sidecars(canonical)
        if rollback.exists():
            os.replace(rollback, canonical)
            _move_database_sidecars(rollback, canonical)
        if stage.exists():
            stage.unlink()
        raise SkillApiBlocked("upgrade failed; pre-upgrade state was restored") from exc

    final_inspection = _inspect_migrations(canonical, expected)
    checks = {
        "pre_upgrade_backup": pre_backup.backup_path.is_file(),
        "online_staging": True,
        "migration_applied": not bool(final_inspection["pending"]),
        "target_verified": True,
        "rollback_preserved": rollback.is_file(),
    }
    state_path = profile.workspace_root / profile.state_file
    state = _read_install_state(state_path) or {}
    state.update({
        "last_upgrade_plan_id": plan.plan_id,
        "last_upgrade_at": datetime.now(timezone.utc).isoformat(),
        "last_upgrade_backup": str(pre_backup.backup_path),
        "schema_head": final_inspection["schema_head"],
    })
    _write_text_atomic(state_path, json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return UpgradeResult(
        status="applied",
        plan_id=plan.plan_id,
        canonical_db=canonical,
        current_schema_head=final_inspection["schema_head"],
        target_schema_head=plan.target_schema_head,
        pre_upgrade_backup=pre_backup.backup_path,
        rollback_db=rollback,
        checks=checks,
    )


class SelfTestResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    status: Literal["passed", "failed"] = "passed"
    checks: dict[str, bool] = Field(default_factory=dict)
    cleanup_verified: bool = True
    message: str = "temporary self-test completed and cleaned"


class InstallPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    contract_version: Literal["database-installer-core-v1"] = "database-installer-core-v1"
    status: Literal["planned", "blocked"] = "planned"
    plan_id: str
    confirmation_token: str
    workspace_root: Path
    canonical_db: Path
    profile_path: Path
    workspace_id: str
    project_id: str
    domain_profile: str
    owner_principal_id: str
    checks: dict[str, bool] = Field(default_factory=dict)
    blocked_reasons: tuple[str, ...] = ()


class InstallResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    status: Literal["installed"] = "installed"
    plan_id: str
    canonical_db: Path
    profile_path: Path
    capabilities: CapabilitySet


class ConnectPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    contract_version: Literal["database-installer-core-v1"] = "database-installer-core-v1"
    status: Literal["planned", "blocked"] = "planned"
    plan_id: str
    confirmation_token: str
    workspace_root: Path
    canonical_db: Path
    profile_path: Path
    workspace_id: str
    project_id: str
    domain_profile: str
    owner_principal_id: str
    environment: str
    portable: bool
    source_sha256: str = ""
    checks: dict[str, bool] = Field(default_factory=dict)
    blocked_reasons: tuple[str, ...] = ()


class ConnectResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    status: Literal["connected"] = "connected"
    plan_id: str
    canonical_db: Path
    profile_path: Path
    capabilities: CapabilitySet
def self_test(manifest_path: str | Path) -> SelfTestResult:
    temporary_path: Path | None = None
    checks: dict[str, bool] = {}
    try:
        with tempfile.TemporaryDirectory(prefix="database-installer-self-test-") as temporary:
            temporary_path = Path(temporary)
            workspace = temporary_path / "workspace"
            plan = demo_plan(workspace_root=workspace, manifest_path=manifest_path)
            checks["plan"] = plan.status == "planned"
            if not checks["plan"]:
                return SelfTestResult(
                    status="failed", checks=checks, cleanup_verified=True,
                    message="self-test plan was blocked",
                )
            applied = demo_apply(plan, confirmation_token=plan.confirmation_token)
            status_result = status(applied.profile_path)
            smoke_result = smoke(applied.profile_path)
            export_result = export_profile(applied.profile_path, temporary_path / "profile-export.json")
            checks.update({
                "apply": applied.status == "applied",
                "status_installed": status_result.installation_status == "installed",
                "smoke": smoke_result.status == "ok",
                "profile_export": export_result.portable,
                "profile_without_secret_values": export_result.secret_refs == (),
            })
        cleanup_verified = temporary_path is not None and not temporary_path.exists()
        return SelfTestResult(
            status="passed" if all(checks.values()) and cleanup_verified else "failed",
            checks=checks,
            cleanup_verified=cleanup_verified,
        )
    except Exception as exc:
        cleanup_verified = temporary_path is None or not temporary_path.exists()
        checks["exception"] = False
        return SelfTestResult(
            status="failed", checks=checks, cleanup_verified=cleanup_verified,
            message=f"self-test failed safely: {type(exc).__name__}",
        )


def sample_plan(*, workspace_root: str | Path, manifest_path: str | Path) -> DemoPlan:
    plan = demo_plan(workspace_root=workspace_root, manifest_path=manifest_path)
    return plan.model_copy(update={"confirmation_token": f"sample:{plan.dataset_id}:{plan.plan_id[:12]}"})


def sample_apply(plan: DemoPlan, *, confirmation_token: str) -> DemoApplyResult:
    return demo_apply(plan, confirmation_token=confirmation_token)
def install_plan(
    *, workspace_root: str | Path, workspace_id: str, project_id: str,
    domain_profile: str, owner_principal_id: str,
) -> InstallPlan:
    paths = _workspace_paths(workspace_root)
    checks = {
        "scope_complete": bool(workspace_id.strip() and project_id.strip() and domain_profile.strip() and owner_principal_id.strip()),
        "target_absent": not paths["canonical_db"].exists(),
        "profile_distinct": paths["profile"] != paths["canonical_db"],
    }
    blocked = [name for name, passed in checks.items() if not passed]
    material = {
        "workspace_root": str(paths["workspace_root"]),
        "workspace_id": workspace_id,
        "project_id": project_id,
        "domain_profile": domain_profile,
        "owner_principal_id": owner_principal_id,
        "canonical_db": str(paths["canonical_db"]),
    }
    plan_id = hashlib.sha256(_canonical_json(material).encode("utf-8")).hexdigest()
    return InstallPlan(
        status="blocked" if blocked else "planned",
        plan_id=plan_id,
        confirmation_token=f"install:{workspace_id}:{project_id}:{plan_id[:12]}",
        workspace_root=paths["workspace_root"],
        canonical_db=paths["canonical_db"],
        profile_path=paths["profile"],
        workspace_id=workspace_id,
        project_id=project_id,
        domain_profile=domain_profile,
        owner_principal_id=owner_principal_id,
        checks=checks,
        blocked_reasons=tuple(dict.fromkeys(blocked)),
    )


def install_apply(plan: InstallPlan, *, confirmation_token: str) -> InstallResult:
    if plan.status != "planned":
        raise SkillApiBlocked("install plan is blocked: " + ", ".join(plan.blocked_reasons))
    if confirmation_token != plan.confirmation_token:
        raise SkillApiBlocked("install confirmation token does not match plan")
    if plan.canonical_db.exists():
        raise SkillApiBlocked("install target already exists")
    created_db = False
    created_profile = False
    created_state = False
    state_path = plan.workspace_root / DEMO_STATE_REL
    try:
        plan.canonical_db.parent.mkdir(parents=True, exist_ok=True)
        plan.profile_path.parent.mkdir(parents=True, exist_ok=True)
        (plan.workspace_root / "runtime").mkdir(parents=True, exist_ok=True)
        (plan.workspace_root / "backups").mkdir(parents=True, exist_ok=True)
        store = SQLiteStore(plan.canonical_db)
        store.migrate()
        created_db = True
        store.ensure_workspace_project(
            workspace_id=plan.workspace_id, workspace_name=plan.workspace_id,
            project_id=plan.project_id, project_name=plan.project_id,
        )
        store.register_profiles(ProfileRegistry())
        capabilities = _profile_capabilities()
        profile = ConnectionProfile(
            mode="real", environment="development", workspace_root=plan.workspace_root,
            capabilities=capabilities, workspace_id=plan.workspace_id,
            project_ids=(plan.project_id,), domain_profile=plan.domain_profile,
            owner_principal_id=plan.owner_principal_id,
            max_sensitivity=SensitivityLevel.PRIVATE,
            created_at=datetime.now(timezone.utc),
        )
        serialized_profile = profile.model_dump_json(indent=2) + "\n"
        scan = scan_for_secrets(serialized_profile, secret_refs=profile.secret_refs)
        if scan.status == "blocked":
            raise SkillApiBlocked("install profile blocked by secret scan")
        _write_text_atomic(plan.profile_path, serialized_profile)
        created_profile = True
        _write_text_atomic(
            state_path,
            json.dumps({
                "installer_contract_version": "database-installer-core-v1",
                "mode": "real",
                "workspace_id": plan.workspace_id,
                "project_id": plan.project_id,
                "installed_at": datetime.now(timezone.utc).isoformat(),
            }, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
        created_state = True
        return InstallResult(
            plan_id=plan.plan_id, canonical_db=plan.canonical_db,
            profile_path=plan.profile_path, capabilities=capabilities,
        )
    except Exception:
        for path, created in ((state_path, created_state), (plan.profile_path, created_profile), (plan.canonical_db, created_db)):
            if created:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
        raise

def connect_plan(
    *, workspace_root: str | Path, canonical_db: str | Path,
    workspace_id: str, project_id: str, domain_profile: str,
    owner_principal_id: str, environment: str = "development",
) -> ConnectPlan:
    root = Path(workspace_root).resolve()
    source = Path(canonical_db).resolve()
    profile_path = _safe_relative(DEMO_PROFILE_REL, root)
    portable = source.is_relative_to(root)
    checks: dict[str, bool] = {
        "source_exists": source.is_file(),
        "profile_distinct": profile_path != source,
        "scope_complete": bool(workspace_id.strip() and project_id.strip() and domain_profile.strip() and owner_principal_id.strip()),
    }
    if checks["source_exists"]:
        connection = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
        try:
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
            checks["integrity"] = connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            checks["schema_migrations"] = "schema_migrations" in tables
            checks["memory_items"] = "memory_items" in tables
            checks["fts"] = "memory_fts" in tables
            checks["scope"] = False
            if {"workspaces", "projects"} <= tables:
                checks["scope"] = connection.execute(
                    "SELECT 1 FROM workspaces WHERE workspace_id=?", (workspace_id,)
                ).fetchone() is not None and connection.execute(
                    "SELECT 1 FROM projects WHERE project_id=? AND workspace_id=?",
                    (project_id, workspace_id),
                ).fetchone() is not None
        finally:
            connection.close()
    blocked = [name for name, passed in checks.items() if not passed]
    source_hash = _sha256_file(source) if source.is_file() else ""
    material = {
        "workspace_root": str(root), "canonical_db": str(source),
        "source_hash": source_hash, "workspace_id": workspace_id,
        "project_id": project_id, "domain_profile": domain_profile,
    }
    plan_id = hashlib.sha256(_canonical_json(material).encode("utf-8")).hexdigest()
    return ConnectPlan(
        status="blocked" if blocked else "planned",
        plan_id=plan_id,
        confirmation_token=f"connect:{workspace_id}:{project_id}:{plan_id[:12]}",
        workspace_root=root, canonical_db=source, profile_path=profile_path,
        workspace_id=workspace_id, project_id=project_id,
        domain_profile=domain_profile, owner_principal_id=owner_principal_id,
        environment=environment, portable=portable, source_sha256=source_hash,
        checks=checks, blocked_reasons=tuple(dict.fromkeys(blocked)),
    )


def connect_apply(plan: ConnectPlan, *, confirmation_token: str) -> ConnectResult:
    if plan.status != "planned":
        raise SkillApiBlocked("connect plan is blocked: " + ", ".join(plan.blocked_reasons))
    if confirmation_token != plan.confirmation_token:
        raise SkillApiBlocked("connect confirmation token does not match plan")
    if not plan.canonical_db.is_file():
        raise SkillApiBlocked("connect source does not exist")
    if plan.source_sha256 and _sha256_file(plan.canonical_db) != plan.source_sha256:
        raise SkillApiBlocked("connect source changed after planning")
    source_ref = str(plan.canonical_db) if not plan.portable else str(plan.canonical_db.relative_to(plan.workspace_root))
    capabilities = _profile_capabilities()
    profile = ConnectionProfile(
        mode="real", environment=plan.environment, workspace_root=plan.workspace_root,
        canonical_db=source_ref, portable=plan.portable, capabilities=capabilities,
        workspace_id=plan.workspace_id, project_ids=(plan.project_id,),
        domain_profile=plan.domain_profile, owner_principal_id=plan.owner_principal_id,
        max_sensitivity=SensitivityLevel.PRIVATE,
        created_at=datetime.now(timezone.utc),
    )
    _write_text_atomic(plan.profile_path, profile.model_dump_json(indent=2) + "\n")
    _write_text_atomic(
        plan.workspace_root / DEMO_STATE_REL,
        json.dumps({
            "installer_contract_version": "database-installer-core-v1",
            "mode": "connected",
            "canonical_sha256": _sha256_file(plan.canonical_db),
            "connected_at": datetime.now(timezone.utc).isoformat(),
        }, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    return ConnectResult(
        plan_id=plan.plan_id, canonical_db=plan.canonical_db,
        profile_path=plan.profile_path, capabilities=capabilities,
    )