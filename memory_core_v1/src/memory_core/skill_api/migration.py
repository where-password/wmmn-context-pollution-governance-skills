from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .attach import load_database_profile
from .models import DatabaseProfile, SkillApiBlocked, SkillBackend
from .query import health_check


class MigrationCounts(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    active: int = 0
    fts: int = 0
    evidence: int = 0
    migrations: int = 0


class MigrationPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_version: Literal["skill-migration-v1"] = "skill-migration-v1"
    status: Literal["planned", "blocked"] = "planned"
    plan_id: str
    confirmation_token: str
    mode: Literal["copy_only"] = "copy_only"
    source_db: Path
    target_db: Path
    source_sha256: str
    workspace_id: str
    project_id: str
    domain_profile: str
    checks: dict[str, bool] = Field(default_factory=dict)
    blocked_reasons: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class MigrationVerification(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    verified: bool
    source_sha256: str
    target_sha256: str
    integrity: str
    foreign_key_violations: int
    counts: MigrationCounts
    checks: dict[str, bool] = Field(default_factory=dict)


class MigrationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["applied"] = "applied"
    plan_id: str
    source_db: Path
    target_db: Path
    verification: MigrationVerification


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _open_readonly(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=30000")
    return connection


def _counts(path: Path, source: DatabaseProfile, project_id: str) -> MigrationCounts:
    connection = _open_readonly(path)
    try:
        tables = {row["name"] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        active = connection.execute(
            """SELECT COUNT(*) FROM memory_items
               WHERE workspace_id=? AND project_id=? AND domain_profile=?
                 AND status='active' AND deleted_at IS NULL""",
            (source.workspace_id, project_id, source.domain_profile),
        ).fetchone()[0] if "memory_items" in tables else 0
        fts = connection.execute("SELECT COUNT(*) FROM memory_fts").fetchone()[0] if "memory_fts" in tables else 0
        evidence = connection.execute("SELECT COUNT(*) FROM memory_evidence").fetchone()[0] if "memory_evidence" in tables else 0
        migrations = connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] if "schema_migrations" in tables else 0
        return MigrationCounts(active=int(active), fts=int(fts), evidence=int(evidence), migrations=int(migrations))
    finally:
        connection.close()


def _verify_copy(plan: MigrationPlan) -> MigrationVerification:
    source = plan.source_db.resolve()
    target = plan.target_db.resolve()
    checks: dict[str, bool] = {
        "source_exists": source.is_file(),
        "target_exists": target.is_file(),
    }
    if not all(checks.values()):
        return MigrationVerification(
            verified=False,
            source_sha256=_sha256_file(source) if source.is_file() else "",
            target_sha256=_sha256_file(target) if target.is_file() else "",
            integrity="missing",
            foreign_key_violations=-1,
            counts=MigrationCounts(),
            checks=checks,
        )
    source_hash = _sha256_file(source)
    target_hash = _sha256_file(target)
    connection = _open_readonly(target)
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_keys = len(connection.execute("PRAGMA foreign_key_check").fetchall())
        counts = _counts(target, DatabaseProfile(
            environment="verify",
            backend=SkillBackend.SQLITE,
            capabilities=("health",),
            workspace_id=plan.workspace_id,
            project_ids=(plan.project_id,),
            domain_profile=plan.domain_profile,
            canonical_db=target,
            canonical_sha256=target_hash,
            plan_id=plan.plan_id,
            created_at=datetime.now(timezone.utc),
        ), plan.project_id)
    finally:
        connection.close()
    checks.update({
        "source_hash_unchanged": source_hash == plan.source_sha256,
        "copy_hash_matches": source_hash == target_hash,
        "integrity_ok": integrity == "ok",
        "foreign_keys_ok": foreign_keys == 0,
        "schema_present": counts.migrations > 0,
    })
    return MigrationVerification(
        verified=all(checks.values()),
        source_sha256=source_hash,
        target_sha256=target_hash,
        integrity=integrity,
        foreign_key_violations=foreign_keys,
        counts=counts,
        checks=checks,
    )


def plan_migration(profile: DatabaseProfile | str | Path, *, target_db: str | Path) -> MigrationPlan:
    profile_value = profile if isinstance(profile, DatabaseProfile) else load_database_profile(profile)
    if profile_value.backend != SkillBackend.SQLITE:
        raise SkillApiBlocked("V1 migration supports sqlite profiles only")
    if "migrate" not in profile_value.capabilities:
        raise SkillApiBlocked("profile capabilities do not allow migrate")
    project = profile_value.project_ids[0]
    source = profile_value.canonical_db.resolve()
    target = Path(target_db).resolve()
    checks = {
        "source_exists": source.is_file(),
        "target_distinct": target != source,
        "target_absent": not target.exists(),
    }
    blocked = [name for name, passed in checks.items() if not passed]
    warnings: list[str] = []
    source_health = health_check(profile_value, project_id=project)
    checks["source_quality_not_critical"] = source_health.quality_status != "critical"
    checks["source_consistency_not_critical"] = source_health.consistency_status != "critical"
    for name in ("source_quality_not_critical", "source_consistency_not_critical"):
        if not checks[name]:
            blocked.append(name)
    warnings.extend(source_health.warnings)
    source_hash = _sha256_file(source) if checks["source_exists"] else ""
    plan_material = {
        "source_sha256": source_hash,
        "source_db": str(source),
        "target_db": str(target),
        "workspace_id": profile_value.workspace_id,
        "project_id": project,
        "domain_profile": profile_value.domain_profile,
    }
    plan_id = hashlib.sha256(
        json.dumps(plan_material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return MigrationPlan(
        status="blocked" if blocked else "planned",
        plan_id=plan_id,
        confirmation_token=f"migrate:{profile_value.workspace_id}:{project}:{plan_id[:12]}",
        source_db=source,
        target_db=target,
        source_sha256=source_hash,
        workspace_id=profile_value.workspace_id,
        project_id=project,
        domain_profile=profile_value.domain_profile,
        checks=checks,
        blocked_reasons=tuple(dict.fromkeys(blocked)),
        warnings=tuple(dict.fromkeys(warnings)),
    )


def apply_migration(
    profile: DatabaseProfile | str | Path,
    plan: MigrationPlan,
    *,
    confirmation_token: str,
) -> MigrationResult:
    profile_value = profile if isinstance(profile, DatabaseProfile) else load_database_profile(profile)
    if profile_value.backend != SkillBackend.SQLITE:
        raise SkillApiBlocked("V1 migration supports sqlite profiles only")
    if "migrate" not in profile_value.capabilities:
        raise SkillApiBlocked("profile capabilities do not allow migrate")
    if plan.status != "planned":
        raise SkillApiBlocked("migration plan is blocked: " + ", ".join(plan.blocked_reasons))
    if confirmation_token != plan.confirmation_token:
        raise SkillApiBlocked("migration confirmation token does not match plan")
    source = plan.source_db.resolve()
    target = plan.target_db.resolve()
    if source != profile_value.canonical_db.resolve():
        raise SkillApiBlocked("migration source does not match the attached canonical")
    if not source.is_file() or _sha256_file(source) != plan.source_sha256:
        raise SkillApiBlocked("migration source changed after plan was created")
    if target.exists():
        raise SkillApiBlocked("migration target already exists; copy-only migration never overwrites")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.migration-{uuid.uuid4().hex}.tmp")
    try:
        shutil.copy2(source, temporary)
        verification = _verify_copy(plan.model_copy(update={"target_db": temporary}))
        if not verification.verified:
            raise SkillApiBlocked("migration staging verification failed")
        os.replace(temporary, target)
        final_verification = _verify_copy(plan)
        if not final_verification.verified:
            raise SkillApiBlocked("migration target verification failed")
    except Exception as exc:
        if temporary.exists():
            temporary.unlink()
        if target.exists():
            target.unlink()
        raise SkillApiBlocked("migration failed; target was not accepted") from exc
    return MigrationResult(
        plan_id=plan.plan_id,
        source_db=source,
        target_db=target,
        verification=final_verification,
    )


def verify_migration(plan: MigrationPlan) -> MigrationVerification:
    return _verify_copy(plan)