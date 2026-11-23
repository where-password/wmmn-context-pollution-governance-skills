from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .attach import load_database_profile
from .models import DatabaseProfile, SkillApiBlocked, SkillBackend


class BackupCounts(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    active: int = 0
    fts: int = 0
    evidence: int = 0
    migrations: int = 0


class BackupManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_version: Literal["skill-backup-v1"] = "skill-backup-v1"
    backup_id: str
    created_at: datetime
    source_db: Path
    backup_path: Path
    backup_sha256: str
    size_bytes: int
    schema_head: str | None = None
    counts: BackupCounts


class BackupVerification(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    verified: bool
    integrity: str
    foreign_key_violations: int
    counts: BackupCounts
    checks: dict[str, bool] = Field(default_factory=dict)


class BackupResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["created"] = "created"
    manifest: BackupManifest
    verification: BackupVerification
    removed: tuple[str, ...] = ()


class BackupListResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["ok"] = "ok"
    backup_dir: Path
    manifests: tuple[BackupManifest, ...] = ()


class RestoreResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["applied"] = "applied"
    plan_id: str
    target_db: Path
    pre_restore_backup: Path
    rollback_db: Path
    backup_sha256: str
    checks: dict[str, bool] = Field(default_factory=dict)


class RestorePlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_version: Literal["skill-restore-plan-v1"] = "skill-restore-plan-v1"
    status: Literal["planned", "blocked"] = "planned"
    plan_id: str
    confirmation_token: str
    backend: SkillBackend
    source_backup: Path
    target_db: Path
    backup_sha256: str
    target_current_sha256: str | None = None
    checks: dict[str, bool] = Field(default_factory=dict)
    blocked_reasons: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    apply_available: bool = False


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


def _has_git_ancestor(path: Path) -> bool:
    current = path.resolve()
    for parent in (current, *current.parents):
        if (parent / ".git").exists():
            return True
    return False


def _safe_backup_dir(backup_dir: str | Path, canonical: Path) -> Path:
    directory = Path(backup_dir).resolve()
    if directory == canonical.resolve().parent:
        raise SkillApiBlocked("backup directory must not be the canonical database directory")
    if _has_git_ancestor(directory):
        raise SkillApiBlocked("backup directory must not be inside a Git work tree")
    return directory


def _counts(path: Path) -> BackupCounts:
    connection = _open_readonly(path)
    try:
        tables = {row["name"] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        active = connection.execute(
            "SELECT COUNT(*) FROM memory_items WHERE status='active' AND deleted_at IS NULL"
        ).fetchone()[0] if "memory_items" in tables else 0
        fts = connection.execute("SELECT COUNT(*) FROM memory_fts").fetchone()[0] if "memory_fts" in tables else 0
        evidence = connection.execute("SELECT COUNT(*) FROM memory_evidence").fetchone()[0] if "memory_evidence" in tables else 0
        migrations = connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] if "schema_migrations" in tables else 0
        return BackupCounts(active=int(active), fts=int(fts), evidence=int(evidence), migrations=int(migrations))
    finally:
        connection.close()


def _schema_head(path: Path) -> str | None:
    with _open_readonly(path) as connection:
        if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'").fetchone() is None:
            return None
        rows = [row[0] for row in connection.execute("SELECT migration_id FROM schema_migrations ORDER BY migration_id")]
        return rows[-1] if rows else None


def verify_backup(backup_path: str | Path) -> BackupVerification:
    path = Path(backup_path).resolve()
    if not path.is_file():
        return BackupVerification(
            verified=False, integrity="missing", foreign_key_violations=-1,
            counts=BackupCounts(), checks={"backup_exists": False},
        )
    connection = None
    try:
        connection = _open_readonly(path)
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        fk = len(connection.execute("PRAGMA foreign_key_check").fetchall())
        connection.close()
        connection = None
        counts = _counts(path)
    except sqlite3.Error:
        return BackupVerification(
            verified=False,
            integrity="invalid",
            foreign_key_violations=-1,
            counts=BackupCounts(),
            checks={"backup_exists": True, "sqlite_readable": False},
        )
    finally:
        if connection is not None:
            connection.close()
    checks = {
        "backup_exists": True,
        "sqlite_readable": True,
        "integrity_ok": integrity == "ok",
        "foreign_keys_ok": fk == 0,
        "schema_present": counts.migrations > 0,
    }
    return BackupVerification(
        verified=all(checks.values()),
        integrity=integrity,
        foreign_key_violations=fk,
        counts=counts,
        checks=checks,
    )


def create_backup(
    profile: DatabaseProfile | str | Path,
    *,
    backup_dir: str | Path,
    retention: int = 7,
) -> BackupResult:
    profile_value = profile if isinstance(profile, DatabaseProfile) else load_database_profile(profile)
    if profile_value.backend != SkillBackend.SQLITE:
        raise SkillApiBlocked("V1 SQLite backup phase does not support milvus profiles")
    if "backup" not in profile_value.capabilities:
        raise SkillApiBlocked("profile capabilities do not allow backup")
    if retention < 7:
        raise SkillApiBlocked("real SQLite backup retention must be at least 7")
    canonical = profile_value.canonical_db.resolve()
    if not canonical.is_file():
        raise SkillApiBlocked("canonical database does not exist")
    directory = _safe_backup_dir(backup_dir, canonical)
    directory.mkdir(parents=True, exist_ok=True)
    backup_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = directory / f"memory_{backup_id}.sqlite3"
    manifest_path = directory / f"memory_{backup_id}.manifest.json"
    if backup_path.exists() or manifest_path.exists():
        backup_id = f"{backup_id}-{uuid.uuid4().hex[:8]}"
        backup_path = directory / f"memory_{backup_id}.sqlite3"
        manifest_path = directory / f"memory_{backup_id}.manifest.json"
    source_hash_before = _sha256_file(canonical)
    source_connection = sqlite3.connect(f"file:{canonical.as_posix()}?mode=ro", uri=True)
    target_connection = sqlite3.connect(backup_path)
    try:
        source_connection.backup(target_connection, pages=1000)
        target_connection.commit()
    finally:
        target_connection.close()
        source_connection.close()
    if _sha256_file(canonical) != source_hash_before:
        raise SkillApiBlocked("canonical database changed during backup")
    verification = verify_backup(backup_path)
    manifest = BackupManifest(
        backup_id=backup_id,
        created_at=datetime.now(timezone.utc),
        source_db=canonical,
        backup_path=backup_path,
        backup_sha256=_sha256_file(backup_path),
        size_bytes=backup_path.stat().st_size,
        schema_head=_schema_head(backup_path),
        counts=verification.counts,
    )
    temporary = manifest_path.with_name(f"{manifest_path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(manifest.model_dump_json(indent=2) + "\n", encoding="utf-8", newline="\n")
    os.replace(temporary, manifest_path)
    backups = sorted(directory.glob("memory_*.sqlite3"), key=lambda item: item.name, reverse=True)
    keep = set(backups[:retention])
    removed: list[str] = []
    for old in backups[retention:]:
        old_manifest = old.with_name(old.stem + ".manifest.json")
        if old.resolve().parent != directory or old_manifest.resolve().parent != directory:
            raise SkillApiBlocked("unsafe retention path")
        old.unlink()
        if old_manifest.exists():
            old_manifest.unlink()
        removed.append(old.name)
    latest = directory / "latest_manifest.json"
    latest.write_text(manifest.model_dump_json(indent=2) + "\n", encoding="utf-8", newline="\n")
    return BackupResult(manifest=manifest, verification=verification, removed=tuple(removed))


def list_backups(backup_dir: str | Path) -> BackupListResult:
    directory = Path(backup_dir).resolve()
    if not directory.exists():
        return BackupListResult(backup_dir=directory)
    manifests: list[BackupManifest] = []
    for path in directory.glob("memory_*.manifest.json"):
        try:
            manifests.append(BackupManifest.model_validate_json(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    manifests.sort(key=lambda item: item.created_at, reverse=True)
    return BackupListResult(backup_dir=directory, manifests=tuple(manifests))


def plan_restore(
    profile: DatabaseProfile | str | Path,
    *,
    backup_path: str | Path,
    target_db: str | Path | None = None,
) -> RestorePlan:
    profile_value = profile if isinstance(profile, DatabaseProfile) else load_database_profile(profile)
    if profile_value.backend != SkillBackend.SQLITE:
        raise SkillApiBlocked("V1 SQLite restore planning does not support milvus profiles")
    if "restore" not in profile_value.capabilities:
        raise SkillApiBlocked("profile capabilities do not allow restore")
    backup = Path(backup_path).resolve()
    target = Path(target_db).resolve() if target_db else profile_value.canonical_db.resolve()
    checks: dict[str, bool] = {"backup_exists": backup.is_file(), "target_distinct": target != backup}
    blocked: list[str] = []
    warnings: list[str] = []
    if not checks["backup_exists"]:
        blocked.append("backup_exists")
    if not checks["target_distinct"]:
        blocked.append("target_distinct")
    verification = verify_backup(backup)
    checks["backup_verified"] = verification.verified
    if not verification.verified:
        blocked.append("backup_verified")
    target_hash = _sha256_file(target) if target.is_file() else None
    if target_hash is None:
        warnings.append("target database does not exist; restore apply will require explicit target confirmation")
    plan_id = hashlib.sha256(
        json.dumps({
            "backup_sha256": _sha256_file(backup) if backup.is_file() else None,
            "target_db": str(target),
            "target_hash": target_hash,
        }, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return RestorePlan(
        status="blocked" if blocked else "planned",
        plan_id=plan_id,
        confirmation_token=f"restore:{plan_id[:12]}",
        backend=profile_value.backend,
        source_backup=backup,
        target_db=target,
        backup_sha256=_sha256_file(backup) if backup.is_file() else "",
        target_current_sha256=target_hash,
        checks=checks,
        blocked_reasons=tuple(dict.fromkeys(blocked)),
        warnings=tuple(warnings),
        apply_available=not blocked and target.is_file(),
    )
def _lock_is_active(lock_file: str | Path | None) -> bool:
    if lock_file is None:
        return False
    path = Path(lock_file).resolve()
    if not path.exists():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return True
    return str(payload.get("status", "")).casefold() in {"running", "acquired", "held"}


def _move_sidecars(source: Path, target: Path) -> None:
    for suffix in ("-wal", "-shm"):
        src = Path(str(source) + suffix)
        dst = Path(str(target) + suffix)
        if src.exists():
            os.replace(src, dst)


def _restore_sidecars(source: Path, target: Path) -> None:
    for suffix in ("-wal", "-shm"):
        src = Path(str(source) + suffix)
        dst = Path(str(target) + suffix)
        if src.exists():
            os.replace(src, dst)


def apply_restore(
    profile: DatabaseProfile | str | Path,
    plan: RestorePlan,
    *,
    confirmation_token: str,
    pre_backup_dir: str | Path,
    lock_file: str | Path | None = None,
) -> RestoreResult:
    profile_value = profile if isinstance(profile, DatabaseProfile) else load_database_profile(profile)
    if profile_value.backend != SkillBackend.SQLITE:
        raise SkillApiBlocked("restore apply is only available for sqlite profiles")
    if "restore" not in profile_value.capabilities:
        raise SkillApiBlocked("profile capabilities do not allow restore")
    if plan.status != "planned":
        raise SkillApiBlocked("restore plan is blocked: " + ", ".join(plan.blocked_reasons))
    if confirmation_token != plan.confirmation_token:
        raise SkillApiBlocked("restore confirmation token does not match plan")
    target = profile_value.canonical_db.resolve()
    if plan.target_db.resolve() != target:
        raise SkillApiBlocked("restore target must be the attached canonical database")
    if not target.is_file():
        raise SkillApiBlocked("restore target must already exist for V1 apply")
    if _lock_is_active(lock_file):
        raise SkillApiBlocked("an active worker lock prevents restore")

    backup = plan.source_backup.resolve()
    if not backup.is_file() or _sha256_file(backup) != plan.backup_sha256:
        raise SkillApiBlocked("backup changed after restore plan was created")
    if plan.target_current_sha256 is None or _sha256_file(target) != plan.target_current_sha256:
        raise SkillApiBlocked("target database changed after restore plan was created")

    pre_backup = create_backup(profile_value, backup_dir=pre_backup_dir, retention=7)
    stage = target.with_name(f".{target.name}.restore-{uuid.uuid4().hex}.tmp")
    rollback = target.with_name(f"{target.name}.pre-restore-{plan.plan_id[:8]}-{uuid.uuid4().hex}.sqlite3")
    had_target = target.exists()
    try:
        shutil.copy2(backup, stage)
        stage_verification = verify_backup(stage)
        if not stage_verification.verified:
            raise SkillApiBlocked("restore staging verification failed")
        if _sha256_file(backup) != plan.backup_sha256 or _sha256_file(target) != plan.target_current_sha256:
            raise SkillApiBlocked("canonical or backup changed during restore staging")
        if had_target:
            os.replace(target, rollback)
            _move_sidecars(target, rollback)
        os.replace(stage, target)
        target_verification = verify_backup(target)
        if not target_verification.verified:
            raise SkillApiBlocked("restored database verification failed")
    except Exception as exc:
        if target.exists():
            target.unlink()
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(target) + suffix)
            if sidecar.exists():
                sidecar.unlink()
        if had_target and rollback.exists():
            os.replace(rollback, target)
            _restore_sidecars(rollback, target)
        if stage.exists():
            stage.unlink()
        raise SkillApiBlocked("restore failed; previous database was restored when available") from exc
    checks = {
        "plan_valid": True,
        "pre_restore_backup": pre_backup.manifest.backup_path.is_file(),
        "staging_verified": True,
        "target_verified": True,
        "rollback_preserved": rollback.is_file(),
    }
    return RestoreResult(
        plan_id=plan.plan_id,
        target_db=target,
        pre_restore_backup=pre_backup.manifest.backup_path,
        rollback_db=rollback,
        backup_sha256=plan.backup_sha256,
        checks=checks,
    )