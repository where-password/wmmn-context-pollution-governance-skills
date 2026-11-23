from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..db import MIGRATIONS_DIR
from ..routing import (
    GatewayRoute,
    GatewayRouteRegistry,
    RouteCapability,
    RoutingError,
)
from .models import (
    DatabaseProfile,
    ProfileAttachPlan,
    ProfileAttachResult,
    SkillApiBlocked,
    SkillBackend,
)

if TYPE_CHECKING:
    from .installer import ConnectionProfile

READ_CAPABILITIES = {
    RouteCapability.QUERY,
    RouteCapability.HEALTH,
    RouteCapability.BACKUP,
}
SQLITE_CAPABILITIES = READ_CAPABILITIES | {
    RouteCapability.WRITE,
    RouteCapability.RESTORE,
    RouteCapability.MIGRATE,
}


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


@contextmanager
def _readonly_connection(path: Path):
    connection = _open_readonly(path)
    try:
        yield connection
    finally:
        connection.close()


def _migration_ids() -> set[str]:
    return {path.stem for path in MIGRATIONS_DIR.glob("*.sql")}


def _route_capabilities(route: GatewayRoute, backend: SkillBackend) -> tuple[str, ...]:
    allowed = (
        READ_CAPABILITIES | {RouteCapability.WRITE}
        if backend == SkillBackend.MILVUS
        else SQLITE_CAPABILITIES
    )
    selected = [capability.value for capability in allowed if capability in route.capabilities]
    return tuple(sorted(selected))


def _default_capabilities(backend: SkillBackend) -> tuple[str, ...]:
    allowed = READ_CAPABILITIES | {RouteCapability.WRITE} if backend == SkillBackend.MILVUS else SQLITE_CAPABILITIES
    return tuple(sorted(capability.value for capability in allowed))


def _sample_index_metadata(read_index: Any) -> tuple[dict[str, Any], int]:
    rows = read_index.iter_rows(
        filter="",
        batch_size=1,
        output_fields=[
            "memory_id", "version", "embedding_model",
            "embedding_dim", "index_schema_version",
        ],
    )
    try:
        first = next(iter(rows))
    except StopIteration as exc:
        raise SkillApiBlocked("Milvus collection is empty; cannot verify index metadata") from exc
    finally:
        close = getattr(rows, "close", None)
        if close is not None:
            close()
    return dict(first), int(read_index.count())


def build_profile_attach_plan(
    *,
    canonical_db: str | Path,
    profile_path: str | Path,
    workspace_id: str,
    project_id: str,
    domain_profile: str,
    backend: SkillBackend | str,
    environment: str = "development",
    route_path: str | Path | None = None,
    read_index: Any | None = None,
    owner_principal_id: str | None = None,
    allowed_visibilities: tuple[str, ...] = ("private", "workspace", "shared"),
    max_sensitivity: str = "private",
) -> ProfileAttachPlan:
    """Build a read-only attach plan and atomically writable local profile target."""

    backend_value = SkillBackend(backend)
    canonical = Path(canonical_db).resolve()
    profile = Path(profile_path).resolve()
    checks: dict[str, bool] = {}
    warnings: list[str] = []
    blocked: list[str] = []
    route: GatewayRoute | None = None

    checks["canonical_exists"] = canonical.is_file()
    checks["profile_path_distinct"] = profile != canonical
    checks["profile_path_not_directory"] = not profile.is_dir()
    if not checks["canonical_exists"]:
        blocked.append("canonical_exists")
    if not checks["profile_path_distinct"]:
        blocked.append("profile_path_distinct")
    if not checks["profile_path_not_directory"]:
        blocked.append("profile_path_not_directory")

    integrity = "not_checked"
    foreign_key_violations = -1
    migrations_applied: list[str] = []
    if checks["canonical_exists"]:
        try:
            with _readonly_connection(canonical) as connection:
                tables = {
                    row["name"] for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
                    )
                }
                checks["schema_migrations_table"] = "schema_migrations" in tables
                checks["workspace_table"] = "workspaces" in tables
                checks["project_table"] = "projects" in tables
                checks["memory_items_table"] = "memory_items" in tables
                checks["fts_table"] = "memory_fts" in tables
                if "schema_migrations" in tables:
                    migrations_applied = [
                        row["migration_id"] for row in connection.execute(
                            "SELECT migration_id FROM schema_migrations ORDER BY migration_id"
                        )
                    ]
                checks["migrations_complete"] = _migration_ids().issubset(set(migrations_applied))
                integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
                foreign_key_violations = len(connection.execute("PRAGMA foreign_key_check").fetchall())
                checks["integrity_ok"] = integrity == "ok"
                checks["foreign_keys_ok"] = foreign_key_violations == 0
                if all(checks.get(name, False) for name in ("workspace_table", "project_table")):
                    checks["workspace_exists"] = connection.execute(
                        "SELECT 1 FROM workspaces WHERE workspace_id=?", (workspace_id,)
                    ).fetchone() is not None
                    checks["project_scope_exists"] = connection.execute(
                        "SELECT 1 FROM projects WHERE project_id=? AND workspace_id=?",
                        (project_id, workspace_id),
                    ).fetchone() is not None
                else:
                    checks["workspace_exists"] = False
                    checks["project_scope_exists"] = False
                if checks.get("memory_items_table", False):
                    row = connection.execute(
                        """SELECT COUNT(*) FROM memory_items
                           WHERE workspace_id=? AND project_id=? AND domain_profile=?
                             AND status='active' AND deleted_at IS NULL""",
                        (workspace_id, project_id, domain_profile),
                    ).fetchone()
                    active_rows = int(row[0])
                    if active_rows > 0:
                        checks["scope_has_rows"] = True
                    else:
                        warnings.append("scope has no active rows; attach is allowed but health must assess quality")
                else:
                    warnings.append("memory_items table is unavailable; scope row check was skipped")
        except sqlite3.Error as exc:
            checks["canonical_sqlite_readable"] = False
            blocked.append("canonical_sqlite_readable")
            warnings.append(f"canonical SQLite read failed: {type(exc).__name__}")
        else:
            checks["canonical_sqlite_readable"] = True

    for name in (
        "schema_migrations_table", "workspace_table", "project_table",
        "memory_items_table", "fts_table", "migrations_complete",
        "integrity_ok", "foreign_keys_ok", "workspace_exists", "project_scope_exists",
    ):
        if not checks.get(name, False):
            blocked.append(name)

    if route_path is not None:
        try:
            registry = GatewayRouteRegistry.load(route_path)
            route = registry.resolve(workspace_id=workspace_id, domain_profile=domain_profile)
            checks["route_resolves"] = True
            checks["route_canonical_matches"] = route.canonical_db.resolve() == canonical
            checks["route_query_capability"] = RouteCapability.QUERY in route.capabilities
            if not checks["route_canonical_matches"]:
                blocked.append("route_canonical_matches")
            if not checks["route_query_capability"]:
                blocked.append("route_query_capability")
        except (OSError, ValueError, RoutingError) as exc:
            checks["route_resolves"] = False
            route_error = type(exc).__name__
            blocked.append("route_resolves")
    else:
        checks["route_resolves"] = backend_value == SkillBackend.SQLITE
        if not checks["route_resolves"]:
            blocked.append("route_resolves")
        else:
            warnings.append("sqlite attach has no route; route_id/alias fields are absent")

    if backend_value == SkillBackend.MILVUS:
        if read_index is None:
            checks["read_index_available"] = False
            blocked.append("read_index_available")
        else:
            checks["read_index_available"] = True
            try:
                alias_map = read_index.aliases()
                target = alias_map.get(route.read_alias) if route is not None and route.read_alias else None
                checks["read_alias_target_matches"] = (
                    route is not None and target == route.physical_collection
                )
                if not checks["read_alias_target_matches"]:
                    blocked.append("read_alias_target_matches")
                collection = read_index.describe_collection()
                collection_name = collection.get("collection_name") or collection.get("name")
                checks["collection_metadata_available"] = bool(collection)
                if not checks["collection_metadata_available"]:
                    blocked.append("collection_metadata_available")
                if collection_name and route is not None:
                    checks["collection_name_matches"] = collection_name == route.physical_collection
                    if not checks["collection_name_matches"]:
                        blocked.append("collection_name_matches")
                else:
                    checks["collection_name_matches"] = True
                sample, row_count = _sample_index_metadata(read_index)
                checks["index_nonempty"] = row_count > 0
                if not checks["index_nonempty"]:
                    blocked.append("index_nonempty")
                checks["embedding_model_matches"] = (
                    route is not None
                    and sample.get("embedding_model") == route.embedding_model
                )
                checks["embedding_dimension_matches"] = (
                    route is not None
                    and int(sample.get("embedding_dim") or 0) == route.embedding_dimension
                )
                checks["index_schema_version_matches"] = (
                    route is not None
                    and sample.get("index_schema_version") == route.index_schema_version
                )
                for name in (
                    "embedding_model_matches", "embedding_dimension_matches",
                    "index_schema_version_matches",
                ):
                    if not checks.get(name, False):
                        blocked.append(name)
            except (OSError, ValueError, RuntimeError, AttributeError) as exc:
                checks["read_index_checks_complete"] = False
                blocked.append("read_index_checks_complete")
                warnings.append(f"read-only index check failed: {type(exc).__name__}")
            else:
                checks["read_index_checks_complete"] = True

    capabilities = _route_capabilities(route, backend_value) if route is not None else _default_capabilities(backend_value)
    canonical_hash = _sha256_file(canonical) if checks["canonical_exists"] else ""
    plan_material = {
        "canonical_sha256": canonical_hash,
        "workspace_id": workspace_id,
        "project_id": project_id,
        "domain_profile": domain_profile,
        "backend": backend_value.value,
        "environment": environment,
        "route_id": route.route_id if route is not None else None,
        "profile_path": str(profile),
    }
    plan_id = hashlib.sha256(
        json.dumps(plan_material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    confirmation_token = (
        f"attach:{workspace_id}:{project_id}:{backend_value.value}:{plan_id[:12]}"
    )
    status = "blocked" if blocked else "planned"
    return ProfileAttachPlan(
        status=status,
        plan_id=plan_id,
        confirmation_token=confirmation_token,
        environment=environment,
        backend=backend_value,
        workspace_id=workspace_id,
        project_id=project_id,
        domain_profile=domain_profile,
        owner_principal_id=owner_principal_id,
        allowed_visibilities=allowed_visibilities,
        max_sensitivity=max_sensitivity,
        canonical_db=canonical,
        canonical_sha256=canonical_hash,
        route_id=route.route_id if route is not None else None,
        read_alias=route.read_alias if route is not None else None,
        write_alias=route.write_alias if route is not None else None,
        physical_collection=route.physical_collection if route is not None else None,
        embedding_model=route.embedding_model if route is not None else None,
        embedding_dimension=route.embedding_dimension if route is not None else None,
        index_schema_version=route.index_schema_version if route is not None else None,
        metric_type=route.metric_type if route is not None else None,
        profile_path=profile,
        capabilities=capabilities,
        checks=checks,
        blocked_reasons=tuple(dict.fromkeys(blocked)),
        warnings=tuple(warnings),
    )


def apply_profile_attach(plan: ProfileAttachPlan, *, confirmation_token: str) -> ProfileAttachResult:
    """Persist only the redacted local profile after a valid read-only plan."""

    if plan.status != "planned":
        raise SkillApiBlocked("attach plan is blocked: " + ", ".join(plan.blocked_reasons))
    if not plan.checks or not all(plan.checks.values()):
        raise SkillApiBlocked("attach plan has failed checks")
    if confirmation_token != plan.confirmation_token:
        raise SkillApiBlocked("attach confirmation token does not match plan")
    canonical = plan.canonical_db.resolve()
    if not canonical.is_file() or _sha256_file(canonical) != plan.canonical_sha256:
        raise SkillApiBlocked("canonical database changed after attach plan was created")
    profile_path = plan.profile_path.resolve()
    if profile_path == canonical or profile_path.is_dir():
        raise SkillApiBlocked("profile path is not a safe local configuration target")
    payload = {
        "contract_version": "skill-profile-v1",
        "profile_version": "1.0.0",
        "mode": plan.mode,
        "environment": plan.environment,
        "backend": plan.backend.value,
        "capabilities": list(plan.capabilities),
        "workspace_id": plan.workspace_id,
        "project_ids": [plan.project_id],
        "domain_profile": plan.domain_profile,
        "owner_principal_id": plan.owner_principal_id,
        "allowed_visibilities": list(plan.allowed_visibilities),
        "max_sensitivity": plan.max_sensitivity.value,
        "canonical_db": str(canonical),
        "canonical_sha256": plan.canonical_sha256,
        "route_id": plan.route_id,
        "read_alias": plan.read_alias,
        "write_alias": plan.write_alias,
        "physical_collection": plan.physical_collection,
        "embedding_model": plan.embedding_model,
        "embedding_dimension": plan.embedding_dimension,
        "index_schema_version": plan.index_schema_version,
        "metric_type": plan.metric_type,
        "plan_id": plan.plan_id,
        "created_at": plan.created_at.astimezone(timezone.utc).isoformat(),
    }
    rendered = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    previous = profile_path.read_text(encoding="utf-8") if profile_path.is_file() else None
    changed = previous != rendered
    if changed:
        profile_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = profile_path.with_name(f"{profile_path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(rendered, encoding="utf-8", newline="\n")
            os.replace(temporary, profile_path)
        finally:
            if temporary.exists():
                temporary.unlink()
    return ProfileAttachResult(
        plan_id=plan.plan_id,
        profile_path=profile_path,
        changed=changed,
        checks=plan.checks,
        warnings=plan.warnings,
    )
def create_readonly_milvus_index(
    *,
    route_path: str | Path,
    workspace_id: str,
    domain_profile: str,
    uri_env: str = "MILVUS_URI",
) -> Any:
    """Create a read-only Milvus adapter from a route and an environment variable name."""

    registry = GatewayRouteRegistry.load(route_path)
    route = registry.resolve(workspace_id=workspace_id, domain_profile=domain_profile)
    uri = os.environ.get(uri_env)
    if not uri:
        raise SkillApiBlocked(f"environment variable is not configured: {uri_env}")
    from ..readonly_milvus import ReadOnlyMilvusIndex
    try:
        return ReadOnlyMilvusIndex(
            uri=uri,
            read_alias=route.read_alias,
            dimension=route.embedding_dimension,
            metric_type=route.metric_type,
        )
    except RuntimeError as exc:
        raise SkillApiBlocked("read-only Milvus initialization failed") from exc


def connection_profile_to_database_profile(profile: "ConnectionProfile") -> DatabaseProfile:
    """Adapt an installer ConnectionProfile to the read/query DatabaseProfile contract."""

    workspace_root = Path(profile.workspace_root).expanduser().resolve()
    raw_canonical = Path(profile.canonical_db).expanduser()
    if not raw_canonical.is_absolute() and ".." in raw_canonical.parts:
        raise SkillApiBlocked("connection profile canonical_db must not contain parent traversal")
    canonical = raw_canonical if raw_canonical.is_absolute() else workspace_root / raw_canonical
    if canonical.is_symlink():
        raise SkillApiBlocked("connection profile canonical_db must not be a symlink")
    canonical = canonical.resolve(strict=False)
    if not raw_canonical.is_absolute() and not canonical.is_relative_to(workspace_root):
        raise SkillApiBlocked("connection profile canonical_db escapes workspace_root")

    capabilities = {"health"}
    if profile.capabilities.sqlite == "installed":
        capabilities.add("backup")
    if profile.capabilities.sqlite == "installed" and profile.capabilities.fts == "available":
        capabilities.update({"query", "write", "restore", "migrate"})

    profile_material = json.dumps(
        profile.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    plan_id = "connection_profile_" + hashlib.sha256(profile_material.encode("utf-8")).hexdigest()[:32]
    canonical_sha256 = _sha256_file(canonical) if canonical.is_file() else "unavailable"
    return DatabaseProfile(
        mode=profile.mode,
        environment=profile.environment,
        backend=SkillBackend(profile.backend),
        capabilities=tuple(sorted(capabilities)),
        workspace_id=profile.workspace_id,
        project_ids=tuple(profile.project_ids),
        domain_profile=profile.domain_profile,
        owner_principal_id=profile.owner_principal_id,
        allowed_visibilities=tuple(profile.allowed_visibilities),
        max_sensitivity=profile.max_sensitivity,
        canonical_db=canonical,
        canonical_sha256=canonical_sha256,
        plan_id=plan_id,
        created_at=profile.created_at,
    )


def load_database_profile(profile_path: str | Path) -> DatabaseProfile:
    path = Path(profile_path).resolve()
    if not path.is_file():
        raise SkillApiBlocked("database profile does not exist")
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise SkillApiBlocked("database profile is not valid UTF-8 JSON") from exc
    if not isinstance(raw, dict):
        raise SkillApiBlocked("database profile must be a JSON object")
    contract = raw.get("contract_version")
    if contract == "skill-profile-v1":
        try:
            return DatabaseProfile.model_validate(raw)
        except ValueError as exc:
            raise SkillApiBlocked("database profile failed strict validation") from exc
    if contract == "database-installer-profile-v1":
        from .installer import ConnectionProfile

        try:
            connection_profile = ConnectionProfile.model_validate(raw)
            return connection_profile_to_database_profile(connection_profile)
        except SkillApiBlocked:
            raise
        except ValueError as exc:
            raise SkillApiBlocked("connection profile failed strict validation") from exc
    raise SkillApiBlocked("database profile has an unsupported contract_version")