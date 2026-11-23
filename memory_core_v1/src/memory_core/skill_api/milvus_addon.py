from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ..fakes import FakeEmbedding, FakeVectorIndex
from ..readonly_milvus import ReadOnlyMilvusIndex
from ..routing import GatewayRouteRegistry
from .attach import apply_profile_attach, build_profile_attach_plan
from .milvus_bootstrap import (
    MilvusBootstrapPlan,
    MilvusBootstrapResult,
    apply_milvus_bootstrap as _core_apply_milvus_bootstrap,
    plan_milvus_bootstrap as _core_plan_milvus_bootstrap,
)
from .models import DatabaseProfile, ProfileAttachPlan, ProfileAttachResult, SkillApiBlocked, SkillBackend
from .runtime import (
    create_bootstrap_milvus_index,
    create_bootstrap_resource_probe,
    create_default_embedding_client,
    create_readonly_milvus_from_profile,
)

MILVUS_IMAGE = "milvusdb/milvus:v2.6.27"
MILVUS_PROFILE_REL = "config/milvus-profile.json"
MILVUS_COMPOSE_REL = ".milvus/compose.yml"
MILVUS_DATA_REL = ".milvus/data"
MILVUS_PROJECT_PREFIX = "database-installer-core-milvus-"
SAFE_PROFILE_ALIAS = re.compile(r"^(?!.*prod)[A-Za-z0-9_.-]+$", re.IGNORECASE)


class MilvusStatusResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    status: Literal["ready", "degraded", "blocked", "not_configured"]
    vector: Literal["connected", "degraded", "deferred", "disabled"]
    embedding: Literal["verified", "configured_unverified", "not_configured"]
    checks: dict[str, bool] = Field(default_factory=dict)
    details: dict[str, Any] = Field(default_factory=dict)
    warnings: tuple[str, ...] = ()


class LocalMilvusInstallPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    contract_version: Literal["database-installer-milvus-install-v1"] = "database-installer-milvus-install-v1"
    status: Literal["planned", "blocked"] = "planned"
    plan_id: str
    confirmation_token: str
    workspace_root: Path
    compose_template: Path
    compose_target: Path
    data_root: Path
    project_name: str
    image: str = MILVUS_IMAGE
    checks: dict[str, bool] = Field(default_factory=dict)
    secret_refs: tuple[str, ...] = ()
    actions: tuple[str, ...] = ()
    blocked_reasons: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class LocalMilvusInstallResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    status: Literal["started", "already_running"]
    plan_id: str
    project_name: str
    compose_file: Path
    image: str
    checks: dict[str, bool] = Field(default_factory=dict)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _nonprod_name(value: str | None, label: str) -> str:
    name = (value or "").strip()
    if not name or not SAFE_PROFILE_ALIAS.fullmatch(name):
        raise SkillApiBlocked(f"{label} must be a non-empty safe Milvus name")
    lowered = name.casefold()
    if "prod" in lowered or not ("_test_" in lowered or "_staging_" in lowered):
        raise SkillApiBlocked(f"{label} must be a test/staging name and must not contain prod")
    return name


def _nonprod_collection(value: str | None) -> str:
    name = (value or "").strip()
    if not name or not SAFE_PROFILE_ALIAS.fullmatch(name):
        raise SkillApiBlocked("physical_collection must be a safe Milvus name")
    if "prod" in name.casefold():
        raise SkillApiBlocked("physical_collection must not contain prod")
    return name


def _load_connection_profile(profile_path: str | Path):
    from .installer import load_connection_profile
    return load_connection_profile(profile_path)


def _database_profile(profile_path: str | Path) -> DatabaseProfile:
    profile = _load_connection_profile(profile_path)
    canonical = (profile.workspace_root / profile.canonical_db).resolve()
    canonical_hash = _sha256_file(canonical) if canonical.is_file() else "missing"
    return DatabaseProfile(
        environment=profile.environment,
        backend=SkillBackend.SQLITE,
        capabilities=("query", "health", "backup", "restore", "migrate", "write"),
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


def _route(route_path: str | Path, workspace_id: str, domain_profile: str):
    try:
        return GatewayRouteRegistry.load(route_path).resolve(
            workspace_id=workspace_id,
            domain_profile=domain_profile,
        )
    except Exception as exc:
        raise SkillApiBlocked("Milvus route is invalid, selects a production alias, or lacks a test/staging alias") from exc


class _FakeCleanupClient:
    def __init__(self) -> None:
        self.aliases: set[str] = set()
        self.collections: set[str] = set()

    def list_aliases(self):
        return {"aliases": sorted(self.aliases)}

    def drop_alias(self, *, alias):
        self.aliases.discard(alias)

    def has_collection(self, name: str):
        return name in self.collections

    def drop_collection(self, name: str):
        self.collections.discard(name)


class _FakeReadIndex:
    def __init__(self, *, route, count: int = 1) -> None:
        self.route = route
        self._count = count

    def aliases(self):
        return {self.route.read_alias: self.route.physical_collection}

    def describe_collection(self):
        return {"collection_name": self.route.physical_collection, "fields": ["memory_id", "embedding"]}

    def count(self, *, filter: str = ""):
        del filter
        return self._count

    def iter_rows(self, *, filter: str = "", batch_size: int = 1000, output_fields=None):
        del filter, output_fields
        if self._count:
            yield {
                "memory_id": "milvus-probe-memory",
                "version": 1,
                "embedding_model": self.route.embedding_model,
                "embedding_dim": self.route.embedding_dimension,
                "index_schema_version": self.route.index_schema_version,
            }

    def load_state(self):
        return {"state": 3}


class _FakeProbe:
    def __init__(self, *, collection_name: str, aliases: tuple[str, ...]) -> None:
        self.collection_name = collection_name
        self.aliases = set(aliases)

    def has_collection(self, name: str | None = None):
        return (name or self.collection_name) == self.collection_name and False

    def list_aliases(self):
        return {"aliases": sorted(self.aliases)}


class _FakeBootstrapIndex(FakeVectorIndex):
    def __init__(self, *, collection_name: str, read_alias: str, write_alias: str, fail_remaining: int = 0) -> None:
        super().__init__(read_alias=read_alias, write_alias=write_alias, fail_remaining=fail_remaining)
        self.collection_name = collection_name
        self.client = _FakeCleanupClient()
        self.bound = False

    def ensure_collection(self, name: str | None = None):
        if self.client.has_collection(self.collection_name):
            return False
        self.client.collections.add(self.collection_name)
        self.collections.setdefault(self.read_alias, {})
        self.collections.setdefault(self.write_alias, {})
        return True

    def bind_aliases(self, *, switch_if_exists: bool = False):
        if switch_if_exists:
            raise SkillApiBlocked("bootstrap never switches an existing alias")
        for alias in (self.read_alias, self.write_alias):
            if alias in self.client.aliases:
                raise SkillApiBlocked(f"alias already exists: {alias}")
            self.client.aliases.add(alias)
        self.bound = True

    def aliases(self):
        return {self.read_alias: self.collection_name, self.write_alias: self.collection_name}


def _fake_bootstrap(plan: MilvusBootstrapPlan) -> tuple[_FakeBootstrapIndex, FakeEmbedding]:
    index = _FakeBootstrapIndex(
        collection_name=plan.physical_collection,
        read_alias=plan.read_alias,
        write_alias=plan.write_alias,
    )
    return index, FakeEmbedding(dimension=plan.embedding_dimension, model=plan.embedding_model)


def _fake_or_real_read_index(*, route, fake: bool):
    if fake:
        return _FakeReadIndex(route=route)
    uri = os.environ.get("MILVUS_URI")
    if not uri:
        raise SkillApiBlocked("MILVUS_URI is not configured")
    return ReadOnlyMilvusIndex(
        uri=uri,
        read_alias=route.read_alias,
        dimension=route.embedding_dimension,
        metric_type=route.metric_type or "COSINE",
    )


def milvus_status(
    profile_path: str | Path,
    *,
    route_path: str | Path | None = None,
    milvus_profile_path: str | Path | None = None,
    read_index: Any | None = None,
    fake: bool = False,
    docker: dict[str, bool] | None = None,
    milvus_uri_env: str = "MILVUS_URI",
    embedding_key_env: str = "EMBEDDING_API_KEY",
    embedding_model_env: str = "EMBEDDING_MODEL",
    embedding_dim_env: str = "EMBEDDING_DIM",
) -> MilvusStatusResult:
    profile = _load_connection_profile(profile_path)
    source = (profile.workspace_root / profile.canonical_db).resolve()
    checks: dict[str, bool] = {
        "canonical_exists": source.is_file(),
        "milvus_uri_ref_present": milvus_uri_env in os.environ,
        "embedding_key_ref_present": embedding_key_env in os.environ,
    }
    details: dict[str, Any] = {
        "workspace_id": profile.workspace_id,
        "project_id": profile.project_ids[0],
        "domain_profile": profile.domain_profile,
        "milvus_uri_env": milvus_uri_env,
        "embedding_key_env": embedding_key_env,
        "embedding_model": os.environ.get(embedding_model_env, ""),
        "embedding_dim": os.environ.get(embedding_dim_env, ""),
        "milvus_profile_path": str(Path(milvus_profile_path).resolve()) if milvus_profile_path else None,
    }
    warnings: list[str] = []
    route = None
    if route_path is not None:
        try:
            route = _route(route_path, profile.workspace_id, profile.domain_profile)
            _nonprod_name(route.read_alias, "read_alias")
            if route.write_alias:
                _nonprod_name(route.write_alias, "write_alias")
            checks["route_resolves"] = True
            checks["route_aliases_nonprod"] = True
            checks["collection_name_nonprod"] = bool(route.physical_collection) and "prod" not in route.physical_collection.casefold()
            details.update({
                "route_id": route.route_id,
                "read_alias": route.read_alias,
                "write_alias": route.write_alias,
                "physical_collection": route.physical_collection,
                "embedding_model": route.embedding_model,
                "embedding_dim": route.embedding_dimension,
                "index_schema_version": route.index_schema_version,
            })
        except Exception as exc:
            checks["route_resolves"] = False
            checks["route_aliases_nonprod"] = False
            warnings.append(f"Milvus route check failed: {type(exc).__name__}")
    else:
        checks["route_resolves"] = False
        warnings.append("no Milvus route supplied; vector connection is not configured")

    if docker is not None:
        checks.update({
            "docker_cli": bool(docker.get("cli")),
            "docker_engine": bool(docker.get("engine")),
            "compose_plugin": bool(docker.get("compose")),
            "wsl_cli_present": bool(docker.get("wsl")),
        })
    else:
        docker_cli = shutil.which("docker")
        checks["docker_cli"] = docker_cli is not None
        checks["docker_engine"] = False
        checks["compose_plugin"] = False
        if docker_cli:
            engine = subprocess.run([docker_cli, "version", "--format", "{{.Server.Version}}"], capture_output=True, text=True)
            checks["docker_engine"] = engine.returncode == 0
            compose = subprocess.run([docker_cli, "compose", "version"], capture_output=True, text=True)
            checks["compose_plugin"] = compose.returncode == 0
        checks["wsl_cli_present"] = shutil.which("wsl") is not None

    if read_index is None and route is not None and (fake or checks["milvus_uri_ref_present"]):
        try:
            read_index = _fake_or_real_read_index(route=route, fake=fake)
        except Exception as exc:
            warnings.append(f"read-only Milvus initialization failed: {type(exc).__name__}")
    if read_index is not None and route is not None:
        try:
            alias_map = read_index.aliases()
            checks["read_alias_target_matches"] = alias_map.get(route.read_alias) == route.physical_collection
            collection = read_index.describe_collection()
            checks["collection_metadata_available"] = bool(collection)
            metadata = next(iter(read_index.iter_rows(batch_size=1)), None)
            checks["index_nonempty"] = int(read_index.count()) > 0
            if metadata:
                checks["embedding_model_matches"] = metadata.get("embedding_model") == route.embedding_model
                checks["embedding_dim_matches"] = int(metadata.get("embedding_dim") or 0) == route.embedding_dimension
                checks["index_schema_matches"] = metadata.get("index_schema_version") == route.index_schema_version
            details["read_alias_target"] = alias_map.get(route.read_alias)
            details["index_count"] = int(read_index.count())
            details["load_state"] = read_index.load_state()
        except Exception as exc:
            warnings.append(f"read-only Milvus metadata check failed: {type(exc).__name__}")
    else:
        checks["read_alias_target_matches"] = False
        checks["collection_metadata_available"] = False
        checks["index_nonempty"] = False

    vector = "disabled"
    embedding = "not_configured"
    if checks["embedding_key_ref_present"]:
        embedding = "configured_unverified"
    if route is not None and not checks["embedding_key_ref_present"]:
        vector = "deferred"
    elif read_index is not None and all(checks.get(name, False) for name in ("read_alias_target_matches", "collection_metadata_available", "index_nonempty", "embedding_model_matches", "embedding_dim_matches", "index_schema_matches")):
        vector = "connected"
    elif route is not None:
        vector = "degraded"
    route_requested = route_path is not None
    blocked = (
        not checks.get("canonical_exists", False)
        or (route_requested and not checks.get("route_resolves", False))
        or (route is not None and not checks.get("route_aliases_nonprod", False))
        or (route is not None and not checks.get("collection_name_nonprod", False))
    )
    if blocked:
        status = "blocked"
    elif vector == "connected":
        status = "ready"
    elif vector in {"degraded", "deferred"}:
        status = "degraded" if vector == "degraded" else "not_configured"
    else:
        status = "not_configured"
    return MilvusStatusResult(status=status, vector=vector, embedding=embedding, checks=checks, details=details, warnings=tuple(warnings))


def milvus_connect_plan(
    profile_path: str | Path,
    *,
    route_path: str | Path,
    milvus_profile_path: str | Path | None = None,
    read_index: Any | None = None,
    fake: bool = False,
    environment: str = "test",
) -> ProfileAttachPlan:
    profile = _load_connection_profile(profile_path)
    source = (profile.workspace_root / profile.canonical_db).resolve()
    target = Path(milvus_profile_path).resolve() if milvus_profile_path else (profile.workspace_root / MILVUS_PROFILE_REL).resolve()
    if not target.is_relative_to(profile.workspace_root.resolve()):
        raise SkillApiBlocked("milvus profile must stay inside workspace_root")
    route = _route(route_path, profile.workspace_id, profile.domain_profile)
    _nonprod_name(route.read_alias, "read_alias")
    if route.write_alias:
        _nonprod_name(route.write_alias, "write_alias")
    _nonprod_collection(route.physical_collection)
    if read_index is None:
        read_index = _fake_or_real_read_index(route=route, fake=fake)
    return build_profile_attach_plan(
        canonical_db=source,
        profile_path=target,
        workspace_id=profile.workspace_id,
        project_id=profile.project_ids[0],
        domain_profile=profile.domain_profile,
        backend=SkillBackend.MILVUS,
        environment=environment,
        route_path=route_path,
        read_index=read_index,
        owner_principal_id=profile.owner_principal_id,
        allowed_visibilities=profile.allowed_visibilities,
        max_sensitivity=profile.max_sensitivity,
    )


def milvus_connect_apply(plan: ProfileAttachPlan, *, confirmation_token: str) -> ProfileAttachResult:
    return apply_profile_attach(plan, confirmation_token=confirmation_token)


def _docker_probe() -> dict[str, bool]:
    cli = shutil.which("docker")
    result = {"cli": cli is not None, "engine": False, "compose": False, "wsl": shutil.which("wsl") is not None}
    if cli:
        engine = subprocess.run([cli, "version", "--format", "{{.Server.Version}}"], capture_output=True, text=True)
        result["engine"] = engine.returncode == 0
        compose = subprocess.run([cli, "compose", "version"], capture_output=True, text=True)
        result["compose"] = compose.returncode == 0
    return result


def local_milvus_install_plan(
    *,
    workspace_root: str | Path,
    compose_template: str | Path,
    docker: dict[str, bool] | None = None,
    env_refs: dict[str, bool] | None = None,
) -> LocalMilvusInstallPlan:
    root = Path(workspace_root).resolve()
    template = Path(compose_template).resolve()
    target = (root / MILVUS_COMPOSE_REL).resolve()
    data_root = (root / MILVUS_DATA_REL).resolve()
    checks = {
        "compose_template_exists": template.is_file(),
        "template_is_fixed_milvus_version": MILVUS_IMAGE in template.read_text(encoding="utf-8") if template.is_file() else False,
        "target_not_present": not target.exists(),
        "target_inside_workspace": target.is_relative_to(root),
        "data_root_inside_workspace": data_root.is_relative_to(root),
    }
    state = docker or _docker_probe()
    checks.update({"docker_cli": bool(state.get("cli")), "docker_engine": bool(state.get("engine")), "compose_plugin": bool(state.get("compose"))})
    refs = ("MILVUS_MINIO_USER", "MILVUS_MINIO_PASSWORD")
    ref_state = env_refs or {name: name in os.environ for name in refs}
    checks["minio_user_ref_present"] = bool(ref_state.get("MILVUS_MINIO_USER"))
    checks["minio_password_ref_present"] = bool(ref_state.get("MILVUS_MINIO_PASSWORD"))
    project_name = MILVUS_PROJECT_PREFIX + hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:12]
    blocked = [name for name, passed in checks.items() if not passed]
    material = {"workspace_root": str(root), "project_name": project_name, "template_sha256": _sha256_file(template) if template.is_file() else "", "image": MILVUS_IMAGE}
    plan_id = hashlib.sha256(_canonical_json(material).encode("utf-8")).hexdigest()
    actions = [
        "Review the fixed Compose template and Docker/WSL2 prerequisites.",
        "Apply only with the exact confirmation token.",
        "After startup, run `milvus bootstrap plan/apply` with an explicit test route.",
    ]
    warnings: list[str] = []
    if not checks["docker_engine"]:
        warnings.append("Docker engine is not reachable; Docker/WSL2 installation is never performed by this Skill.")
    if not state.get("wsl"):
        warnings.append("WSL2 CLI was not detected; a working Docker Desktop or equivalent engine is still required.")
    return LocalMilvusInstallPlan(
        status="blocked" if blocked else "planned",
        plan_id=plan_id,
        confirmation_token=f"milvus-install:{plan_id[:12]}",
        workspace_root=root,
        compose_template=template,
        compose_target=target,
        data_root=data_root,
        project_name=project_name,
        checks=checks,
        secret_refs=refs,
        actions=tuple(actions),
        blocked_reasons=tuple(dict.fromkeys(blocked)),
        warnings=tuple(warnings),
    )


def local_milvus_install_apply(
    plan: LocalMilvusInstallPlan,
    *,
    confirmation_token: str,
    runner: Any | None = None,
) -> LocalMilvusInstallResult:
    if plan.status != "planned":
        raise SkillApiBlocked("local Milvus install plan is blocked: " + ", ".join(plan.blocked_reasons))
    if confirmation_token != plan.confirmation_token:
        raise SkillApiBlocked("local Milvus install confirmation token does not match plan")
    if not all(plan.checks.values()):
        raise SkillApiBlocked("local Milvus install prerequisites are not satisfied")
    if plan.image != MILVUS_IMAGE or "prod" in plan.project_name.casefold():
        raise SkillApiBlocked("local Milvus install uses an unsafe image or project name")
    plan.compose_target.parent.mkdir(parents=True, exist_ok=True)
    plan.data_root.mkdir(parents=True, exist_ok=True)
    shutil.copy2(plan.compose_template, plan.compose_target)
    command = ["docker", "compose", "-p", plan.project_name, "-f", str(plan.compose_target), "up", "-d"]
    if runner is None:
        completed = subprocess.run(command, cwd=plan.workspace_root, capture_output=True, text=True)
        exit_code = completed.returncode
    else:
        exit_code = int(runner(command, plan.workspace_root))
    if exit_code != 0:
        raise SkillApiBlocked("local Milvus compose startup failed; no external service was modified")
    return LocalMilvusInstallResult(
        status="started",
        plan_id=plan.plan_id,
        project_name=plan.project_name,
        compose_file=plan.compose_target,
        image=plan.image,
        checks={"compose_copied": plan.compose_target.is_file(), "started": True},
    )


def milvus_bootstrap_plan(
    profile_path: str | Path,
    *,
    route_path: str | Path,
    backend: Literal["fake", "real"] = "fake",
    resource_probe: Any | None = None,
) -> MilvusBootstrapPlan:
    profile = _database_profile(profile_path)
    route = _route(route_path, profile.workspace_id, profile.domain_profile)
    _nonprod_name(route.read_alias, "read_alias")
    _nonprod_name(route.write_alias, "write_alias")
    if resource_probe is not None:
        probe = resource_probe
    elif backend == "fake":
        probe = _FakeProbe(collection_name=route.physical_collection, aliases=())
    else:
        probe = create_bootstrap_resource_probe(
            route_path=route_path,
            workspace_id=profile.workspace_id,
            domain_profile=profile.domain_profile,
        )
    return _core_plan_milvus_bootstrap(profile, route_path=route_path, environment="test", resource_probe=probe)


def milvus_bootstrap_apply(
    profile_path: str | Path,
    plan: MilvusBootstrapPlan,
    *,
    confirmation_token: str,
    backend: Literal["fake", "real"] = "fake",
    vector_index: Any | None = None,
    embedding: Any | None = None,
) -> MilvusBootstrapResult:
    profile = _database_profile(profile_path)
    if backend == "fake":
        fake_index, fake_embedding = _fake_bootstrap(plan)
        vector_index = vector_index or fake_index
        embedding = embedding or fake_embedding
    else:
        vector_index = vector_index or create_bootstrap_milvus_index(plan)
        embedding = embedding or create_default_embedding_client()
    return _core_apply_milvus_bootstrap(
        profile,
        plan,
        confirmation_token=confirmation_token,
        vector_index=vector_index,
        embedding=embedding,
    )


def milvus_reconcile_status(result: MilvusBootstrapResult) -> dict[str, Any]:
    return {
        "status": "zero_difference" if result.differences == 0 and result.verify_passed else "degraded",
        "verify_passed": result.verify_passed,
        "differences": result.differences,
        "expected_counts": result.expected_counts,
        "actual_counts": result.actual_counts,
        "applied_count": result.applied_count,
        "replayed_count": result.replayed_count,
        "skipped_count": result.skipped_count,
    }


def write_generated_test_route(
    *,
    workspace_root: str | Path,
    profile_path: str | Path,
    route_id: str,
    physical_collection: str,
    read_alias: str,
    write_alias: str,
    embedding_model: str = "BAAI/bge-m3",
    embedding_dimension: int = 1024,
    index_schema_version: str = "memory-index-v1",
) -> Path:
    profile = _load_connection_profile(profile_path)
    collection = _nonprod_name(physical_collection, "physical_collection")
    read = _nonprod_name(read_alias, "read_alias")
    write = _nonprod_name(write_alias, "write_alias")
    output = (Path(workspace_root).resolve() / "config" / "milvus-route.json").resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "routes": [{
            "route_id": route_id,
            "workspace_id": profile.workspace_id,
            "domain_profile": profile.domain_profile,
            "canonical_db": str((profile.workspace_root / profile.canonical_db).resolve()),
            "milvus_uri": "http://127.0.0.1:19530",
            "read_alias": read,
            "write_alias": write,
            "physical_collection": collection,
            "allowed_projects": list(profile.project_ids),
            "embedding_model": embedding_model,
            "embedding_dimension": embedding_dimension,
            "index_schema_version": index_schema_version,
            "capabilities": ["query", "health", "backup", "write", "restore", "migrate"],
            "production_read_alias_name": "memory_read_alias_prod",
            "production_write_alias_name": "memory_write_alias_prod",
            "visibility_policy": {
                "require_project_scope": True,
                "require_owner_isolation": True,
                "enforce_sensitivity": True,
                "allow_global_kb": False,
                "allowed_visibilities": list(profile.allowed_visibilities),
            },
        }],
    }
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    return output
