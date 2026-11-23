from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ..db import SQLiteStore
from ..ops import ReconcileService
from ..outbox import OutboxWorker
from ..profiles import ProfileRegistry
from ..routing import GatewayRouteRegistry, RouteCapability
from .attach import load_database_profile
from .models import DatabaseProfile, SkillApiBlocked, SkillBackend
from .query import health_check


class MilvusBootstrapPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_version: Literal["skill-milvus-bootstrap-v1"] = "skill-milvus-bootstrap-v1"
    status: Literal["planned", "blocked"] = "planned"
    plan_id: str
    confirmation_token: str
    environment: Literal["test", "staging"]
    source_db: Path
    workspace_id: str
    project_id: str
    domain_profile: str
    physical_collection: str
    read_alias: str
    write_alias: str
    embedding_model: str
    embedding_dimension: int
    index_schema_version: str
    source_sha256: str
    checks: dict[str, bool] = Field(default_factory=dict)
    blocked_reasons: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class MilvusBootstrapResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["applied"] = "applied"
    plan_id: str
    physical_collection: str
    read_alias: str
    write_alias: str
    applied_count: int
    replayed_count: int
    skipped_count: int
    verify_passed: bool
    expected_counts: dict[str, int] = Field(default_factory=dict)
    actual_counts: dict[str, int] = Field(default_factory=dict)
    differences: int = 0


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _alias_names(value: Any) -> set[str]:
    if isinstance(value, dict):
        return set(value.get("aliases", []))
    return set(value)


def plan_milvus_bootstrap(
    profile: DatabaseProfile | str | Path,
    *,
    route_path: str | Path,
    environment: str = "test",
    resource_probe: Any | None = None,
) -> MilvusBootstrapPlan:
    profile_value = profile if isinstance(profile, DatabaseProfile) else load_database_profile(profile)
    env = environment.casefold()
    if env not in {"test", "staging"}:
        raise SkillApiBlocked("milvus bootstrap is limited to test/staging environments")
    if profile_value.backend != SkillBackend.SQLITE:
        raise SkillApiBlocked("milvus bootstrap requires a sqlite source profile")
    project = profile_value.project_ids[0]
    route = GatewayRouteRegistry.load(route_path).resolve(
        workspace_id=profile_value.workspace_id,
        domain_profile=profile_value.domain_profile,
    )
    checks = {
        "source_is_canonical": route.canonical_db.resolve() == profile_value.canonical_db.resolve(),
        "route_query_capability": RouteCapability.QUERY in route.capabilities,
        "route_write_capability": RouteCapability.WRITE in route.capabilities,
        "test_alias_names": "_test_" in route.read_alias and "_test_" in (route.write_alias or "") and "prod" not in (route.read_alias + (route.write_alias or "")).casefold(),
        "physical_collection_present": bool(route.physical_collection),
        "source_health_available": True,
    }
    warnings: list[str] = []
    if resource_probe is not None:
        checks["collection_absent"] = not bool(resource_probe.has_collection(route.physical_collection))
        aliases = _alias_names(resource_probe.list_aliases())
        checks["aliases_absent"] = route.read_alias not in aliases and (route.write_alias or "") not in aliases
    else:
        warnings.append("no resource probe supplied; collection/alias absence was not verified")
    source_health = health_check(profile_value, project_id=project)
    checks["source_health_not_critical"] = source_health.quality_status != "critical" and source_health.consistency_status != "critical"
    warnings.extend(source_health.warnings)
    blocked = [name for name, passed in checks.items() if not passed]
    source_hash = _sha256_file(profile_value.canonical_db) if profile_value.canonical_db.is_file() else ""
    material = {
        "source_sha256": source_hash,
        "environment": env,
        "physical_collection": route.physical_collection,
        "read_alias": route.read_alias,
        "write_alias": route.write_alias,
    }
    plan_id = hashlib.sha256(json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return MilvusBootstrapPlan(
        status="blocked" if blocked else "planned",
        plan_id=plan_id,
        confirmation_token=f"milvus-bootstrap:{env}:{plan_id[:12]}",
        environment=env,
        source_db=profile_value.canonical_db.resolve(),
        workspace_id=profile_value.workspace_id,
        project_id=project,
        domain_profile=profile_value.domain_profile,
        physical_collection=route.physical_collection,
        read_alias=route.read_alias,
        write_alias=route.write_alias or "",
        embedding_model=route.embedding_model,
        embedding_dimension=route.embedding_dimension,
        index_schema_version=route.index_schema_version,
        source_sha256=source_hash,
        checks=checks,
        blocked_reasons=tuple(dict.fromkeys(blocked)),
        warnings=tuple(dict.fromkeys(warnings)),
    )


def _cleanup_bootstrap_resources(vector_index: Any, plan: MilvusBootstrapPlan) -> bool:
    client = getattr(vector_index, "client", None)
    if client is None:
        return False
    try:
        aliases = client.list_aliases()
        names = set(aliases.get("aliases", []) if isinstance(aliases, dict) else aliases)
        for alias in (plan.read_alias, plan.write_alias):
            if alias in names:
                client.drop_alias(alias=alias)
        if client.has_collection(plan.physical_collection):
            client.drop_collection(plan.physical_collection)
        return True
    except Exception:
        return False


def apply_milvus_bootstrap(
    profile: DatabaseProfile | str | Path,
    plan: MilvusBootstrapPlan,
    *,
    confirmation_token: str,
    vector_index: Any,
    embedding: Any,
) -> MilvusBootstrapResult:
    profile_value = profile if isinstance(profile, DatabaseProfile) else load_database_profile(profile)
    if plan.status != "planned":
        raise SkillApiBlocked("milvus bootstrap plan is blocked: " + ", ".join(plan.blocked_reasons))
    if confirmation_token != plan.confirmation_token:
        raise SkillApiBlocked("milvus bootstrap confirmation token does not match plan")
    source = plan.source_db.resolve()
    if not source.is_file() or _sha256_file(source) != plan.source_sha256:
        raise SkillApiBlocked("source database changed after bootstrap plan was created")
    if plan.environment not in {"test", "staging"}:
        raise SkillApiBlocked("milvus bootstrap is limited to test/staging environments")
    if "prod" in (plan.read_alias + plan.write_alias).casefold():
        raise SkillApiBlocked("production aliases are forbidden in bootstrap")
    created = False
    try:
        created = vector_index.ensure_collection()
        if not created:
            raise SkillApiBlocked("collection already exists; bootstrap never reuses an existing collection")
        vector_index.bind_aliases(switch_if_exists=False)
    except SkillApiBlocked:
        if created:
            _cleanup_bootstrap_resources(vector_index, plan)
        raise
    except Exception as exc:
        if created:
            _cleanup_bootstrap_resources(vector_index, plan)
        raise SkillApiBlocked(f"isolated collection/alias preparation failed: {type(exc).__name__}") from exc
    store = SQLiteStore(source)
    profiles = ProfileRegistry()
    reconciler = ReconcileService(
        store=store,
        vector_index=vector_index,
        workspace_id=plan.workspace_id,
        domain_profile=plan.domain_profile,
        embedding_model=plan.embedding_model,
        embedding_dimension=plan.embedding_dimension,
        index_schema_version=plan.index_schema_version,
        page_size=137,
    )
    outbox = OutboxWorker(
        store=store,
        profiles=profiles,
        embedding=embedding,
        vector_index=vector_index,
        index_schema_version=plan.index_schema_version,
        embedding_model_override=plan.embedding_model,
        embedding_dim_override=plan.embedding_dimension,
        index_schema_version_override=plan.index_schema_version,
    )
    try:
        reconcile_plan = reconciler.plan()
        applied = reconciler.apply(reconcile_plan, explicit=True, worker=outbox, owner="database-skill-bootstrap")
        verification = reconciler.verify(reconcile_plan)
        if not verification.get("passed"):
            raise SkillApiBlocked("bootstrap reconcile verification failed")
    except SkillApiBlocked:
        _cleanup_bootstrap_resources(vector_index, plan)
        raise
    except Exception as exc:
        _cleanup_bootstrap_resources(vector_index, plan)
        raise SkillApiBlocked(f"bootstrap reconcile failed: {type(exc).__name__}") from exc
    return MilvusBootstrapResult(
        plan_id=plan.plan_id,
        physical_collection=plan.physical_collection,
        read_alias=plan.read_alias,
        write_alias=plan.write_alias,
        applied_count=int(applied.get("applied_count", 0)),
        replayed_count=int(applied.get("replayed_count", 0)),
        skipped_count=int(applied.get("skipped_count", 0)),
        verify_passed=bool(verification.get("passed")),
        expected_counts={key: int(value) for key, value in (verification.get("expected_counts") or {}).items()},
        actual_counts={key: int(value) for key, value in (verification.get("actual_counts") or {}).items()},
        differences=len(verification.get("differences") or []),
    )