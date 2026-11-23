from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ..db import SQLiteStore
from ..fakes import FakeEmbedding, FakeVectorIndex
from ..models import (
    AccessGrant,
    AccessScope,
    AssertionSource,
    EvidenceStatus,
    MemoryEvidence,
    MemoryWriteInput,
    SensitivityLevel,
    SourceScope,
    VerificationState,
)
from ..outbox import OutboxWorker
from ..profiles import ProfileRegistry
from ..security import TokenValidator, issue_token
from ..writer import MemoryWriter
from .models import DatabaseProfile, SkillApiBlocked, SkillBackend


class InitPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_version: Literal["skill-init-v1"] = "skill-init-v1"
    status: Literal["planned", "blocked"] = "planned"
    plan_id: str
    confirmation_token: str
    mode: Literal["demo", "real"] = "real"
    environment: str = "development"
    backend: SkillBackend = SkillBackend.SQLITE
    canonical_db: Path
    profile_path: Path
    workspace_id: str
    project_id: str
    domain_profile: str
    owner_principal_id: str | None = None
    checks: dict[str, bool] = Field(default_factory=dict)
    blocked_reasons: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class InitResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["applied"] = "applied"
    plan_id: str
    canonical_db: Path
    profile_path: Path
    mode: Literal["demo", "real"]
    seeded_records: int = 0
    canonical_sha256: str
    checks: dict[str, bool] = Field(default_factory=dict)


DEMO_WORKSPACE = "demo"
DEMO_PROJECT = "demo_project"
DEMO_DOMAIN = "generic"
DEMO_OWNER = "local-demo"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _demo_source_and_units(store: SQLiteStore) -> list[tuple[str, str, str]]:
    source_text = "示例灯塔项目规定每周五进行设备巡检。"
    source_sha = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
    store.insert_source(
        source_id="demo-src-1", workspace_id=DEMO_WORKSPACE, project_id=DEMO_PROJECT,
        visibility="workspace", owner_principal_id=None, source_path="memory://demo-src-1",
        source_sha256=source_sha, source_type="synthetic_demo", sensitivity="private",
    )
    store.insert_source_unit(
        source_unit_id="demo-unit-1", source_id="demo-src-1", parent_unit_id=None,
        scope_type="synthetic_unit", scope_ref="demo-1", source_unit_hash=source_sha,
        text_content=source_text, metadata={},
    )
    return [("demo-unit-1", "demo-src-1", source_text)]


def _seed_demo(store: SQLiteStore) -> int:
    sources = _demo_source_and_units(store)
    now = datetime.now(timezone.utc)
    grant = AccessGrant(
        grant_id="demo-grant", principal_id=DEMO_OWNER, workspace_id=DEMO_WORKSPACE,
        scope=AccessScope(
            project_ids=[DEMO_PROJECT], domains=[DEMO_DOMAIN],
            max_sensitivity=SensitivityLevel.PRIVATE,
        ),
        actions=["write", "search"], expires_at=now + timedelta(minutes=10),
        purpose="synthetic-demo-init", created_at=now,
    )
    store.save_grant(grant)
    secret = secrets.token_bytes(32)
    token = issue_token(
        secret=secret, principal_id=DEMO_OWNER, task_id="demo-task", grant_id=grant.grant_id,
        workspace_id=DEMO_WORKSPACE, ttl_seconds=60,
    )
    profiles = ProfileRegistry()
    writer = MemoryWriter(store=store, profiles=profiles, token_validator=TokenValidator(store, secret=secret))
    for index, (unit_id, source_id, text) in enumerate(sources, 1):
        item = MemoryWriteInput(
            idempotency_key=f"demo-idem-{index}",
            operation_id=f"demo-op-{index}",
            workspace_id=DEMO_WORKSPACE,
            project_id=DEMO_PROJECT,
            domain_profile=DEMO_DOMAIN,
            profile_version="1.0.0",
            memory_type="fact",
            content=text,
            subject="示例巡检",
            importance="medium",
            declared_sensitivity=SensitivityLevel.PRIVATE,
            requested_visibility="workspace",
            assertion_source=AssertionSource.USER_DECISION,
            verification_state=VerificationState.USER_CONFIRMED,
            source_id=source_id,
            source_unit_id=unit_id,
            source_scope=SourceScope(scope_type="synthetic_unit", ref="demo-1"),
            confidence=0.99,
            evidences=[MemoryEvidence(
                evidence_id=f"demo-ev-{index}", source_unit_id=unit_id,
                quote=text, status=EvidenceStatus.VERIFIED,
            )],
        )
        writer.write(item, token)
    OutboxWorker(
        store=store,
        profiles=profiles,
        embedding=FakeEmbedding(dimension=1024),
        vector_index=FakeVectorIndex(),
    ).run_once(owner="database-skill-demo-init", limit=20)
    return len(sources)


def _write_profile(path: Path, profile: DatabaseProfile) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(profile.model_dump_json(indent=2) + "\n", encoding="utf-8", newline="\n")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def plan_init(
    *,
    mode: Literal["demo", "real"],
    canonical_db: str | Path | None,
    profile_path: str | Path,
    backend: SkillBackend | str = SkillBackend.SQLITE,
    environment: str = "development",
    workspace_id: str = "",
    project_id: str = "",
    domain_profile: str = "",
    owner_principal_id: str | None = None,
) -> InitPlan:
    mode_value = mode
    backend_value = SkillBackend(backend)
    profile = Path(profile_path).resolve()
    if mode_value == "demo":
        canonical = Path(canonical_db).resolve() if canonical_db else profile.parent / "demo.sqlite3"
        workspace_id = workspace_id or DEMO_WORKSPACE
        project_id = project_id or DEMO_PROJECT
        domain_profile = domain_profile or DEMO_DOMAIN
        owner_principal_id = owner_principal_id or DEMO_OWNER
        environment = "demo"
    else:
        if canonical_db is None:
            raise SkillApiBlocked("real init requires --canonical-db")
        canonical = Path(canonical_db).resolve()
    checks = {
        "backend_sqlite": backend_value == SkillBackend.SQLITE,
        "canonical_absent": not canonical.exists(),
        "profile_distinct": profile != canonical,
        "scope_complete": bool(
            workspace_id.strip() and project_id.strip() and domain_profile.strip()
            and (mode_value == "demo" or owner_principal_id)
        ),
    }
    blocked = [name for name, passed in checks.items() if not passed]
    material = {
        "mode": mode_value,
        "canonical_db": str(canonical),
        "profile_path": str(profile),
        "workspace_id": workspace_id,
        "project_id": project_id,
        "domain_profile": domain_profile,
        "owner_principal_id": owner_principal_id,
        "backend": backend_value.value,
    }
    plan_id = hashlib.sha256(
        json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return InitPlan(
        status="blocked" if blocked else "planned",
        plan_id=plan_id,
        confirmation_token=f"init:{mode_value}:{workspace_id}:{project_id}:{plan_id[:12]}",
        mode=mode_value,
        environment=environment,
        backend=backend_value,
        canonical_db=canonical,
        profile_path=profile,
        workspace_id=workspace_id,
        project_id=project_id,
        domain_profile=domain_profile,
        owner_principal_id=owner_principal_id,
        checks=checks,
        blocked_reasons=tuple(dict.fromkeys(blocked)),
    )


def apply_init(plan: InitPlan, *, confirmation_token: str) -> InitResult:
    if plan.status != "planned":
        raise SkillApiBlocked("init plan is blocked: " + ", ".join(plan.blocked_reasons))
    if confirmation_token != plan.confirmation_token:
        raise SkillApiBlocked("init confirmation token does not match plan")
    if plan.canonical_db.exists():
        raise SkillApiBlocked("init target already exists")
    plan.canonical_db.parent.mkdir(parents=True, exist_ok=True)
    store = SQLiteStore(plan.canonical_db)
    try:
        store.migrate()
        store.ensure_workspace_project(
            workspace_id=plan.workspace_id,
            workspace_name=plan.workspace_id,
            project_id=plan.project_id,
            project_name=plan.project_id,
        )
        store.register_profiles(ProfileRegistry())
        seeded = _seed_demo(store) if plan.mode == "demo" else 0
    except Exception:
        if plan.canonical_db.exists():
            plan.canonical_db.unlink()
        raise
    canonical_sha = _sha256_file(plan.canonical_db)
    profile = DatabaseProfile(
        mode=plan.mode,
        environment=plan.environment,
        backend=SkillBackend.SQLITE,
        capabilities=("query", "health", "backup", "restore", "migrate"),
        workspace_id=plan.workspace_id,
        project_ids=(plan.project_id,),
        domain_profile=plan.domain_profile,
        owner_principal_id=plan.owner_principal_id,
        max_sensitivity=SensitivityLevel.PRIVATE,
        canonical_db=plan.canonical_db,
        canonical_sha256=canonical_sha,
        plan_id=plan.plan_id,
        created_at=datetime.now(timezone.utc),
    )
    _write_profile(plan.profile_path, profile)
    return InitResult(
        plan_id=plan.plan_id,
        canonical_db=plan.canonical_db,
        profile_path=plan.profile_path,
        mode=plan.mode,
        seeded_records=seeded,
        canonical_sha256=canonical_sha,
        checks={"canonical_created": True, "profile_written": True, "backend_sqlite": True},
    )