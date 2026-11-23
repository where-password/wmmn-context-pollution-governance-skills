from __future__ import annotations

import json
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .models import AccessScope, AgentContextToken, MemoryWriteInput, SearchRequest
from .search import HybridSearchGateway


class RoutingError(RuntimeError):
    pass


class RouteNotFoundError(RoutingError):
    pass


class RouteConfigurationError(RoutingError):
    pass


class RouteAliasError(RoutingError):
    pass


class RouteVisibilityError(RoutingError):
    pass


class RouteCapabilityError(RoutingError):
    pass


class RouteCapability(StrEnum):
    QUERY = "query"
    HEALTH = "health"
    BACKUP = "backup"
    WRITE = "write"
    RESTORE = "restore"
    MIGRATE = "migrate"


class VisibilityPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    require_project_scope: bool = True
    require_owner_isolation: bool = True
    enforce_sensitivity: bool = True
    allow_global_kb: bool = False
    allowed_visibilities: tuple[str, ...] = ("private", "workspace", "shared")


class GatewayRoute(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    route_id: str
    workspace_id: str
    domain_profile: str
    canonical_db: Path
    milvus_uri: str
    read_alias: str
    write_alias: str | None = None
    physical_collection: str
    allowed_projects: tuple[str, ...] = Field(min_length=1)
    embedding_model: str
    embedding_dimension: int = Field(gt=0)
    metric_type: str = "COSINE"
    index_schema_version: str
    visibility_policy: VisibilityPolicy = Field(default_factory=VisibilityPolicy)
    capabilities: frozenset[RouteCapability] = Field(
        default_factory=lambda: frozenset(RouteCapability)
    )
    production_read_alias_name: str | None = None
    production_write_alias_name: str | None = None

    @model_validator(mode="after")
    def validate_route_shape(self) -> "GatewayRoute":
        if not self.route_id or not self.workspace_id or not self.domain_profile:
            raise ValueError("route_id, workspace_id, and domain_profile are required")
        if not self.canonical_db.is_absolute():
            raise ValueError("canonical_db must resolve to an absolute path")
        aliases = [self.read_alias]
        if self.write_alias is not None:
            aliases.append(self.write_alias)
        for alias in aliases:
            if "_test_" not in alias or "prod" in alias.casefold():
                raise ValueError(f"active route alias must be an isolated test alias: {alias}")
        for alias in (self.production_read_alias_name, self.production_write_alias_name):
            if alias is not None and "prod" not in alias.casefold():
                raise ValueError(f"production alias naming convention must contain prod: {alias}")
        if RouteCapability.WRITE in self.capabilities and self.write_alias is None:
            raise ValueError("write capability requires write_alias")
        if self.write_alias is not None and RouteCapability.WRITE not in self.capabilities:
            raise ValueError("write_alias requires write capability")
        if self.write_alias is not None and self.read_alias == self.write_alias:
            raise ValueError("read and write aliases must be distinct")
        if len(set(self.allowed_projects)) != len(self.allowed_projects):
            raise ValueError("allowed_projects must be unique")
        return self


class GatewayRouteRegistry:
    def __init__(self, routes: list[GatewayRoute]) -> None:
        by_id: dict[str, GatewayRoute] = {}
        by_scope: dict[tuple[str, str], GatewayRoute] = {}
        for route in routes:
            if route.route_id in by_id:
                raise RouteConfigurationError(f"duplicate route_id: {route.route_id}")
            key = (route.workspace_id, route.domain_profile)
            if key in by_scope:
                raise RouteConfigurationError(
                    f"duplicate route for workspace/domain: {route.workspace_id}/{route.domain_profile}"
                )
            by_id[route.route_id] = route
            by_scope[key] = route
        self._by_id = by_id
        self._by_scope = by_scope
        self._project_root = None

    @classmethod
    def load(cls, path: str | Path, *, project_root: str | Path | None = None) -> "GatewayRouteRegistry":
        config_path = Path(path).resolve()
        root = Path(project_root).resolve() if project_root is not None else config_path.parents[1]
        payload = json.loads(config_path.read_text(encoding="utf-8"))
        raw_routes = payload.get("routes")
        if not isinstance(raw_routes, list):
            raise RouteConfigurationError("routes must be a list")
        parsed: list[GatewayRoute] = []
        for raw in raw_routes:
            row = dict(raw)
            db_path = Path(row.get("canonical_db", ""))
            if not db_path.is_absolute():
                db_path = (root / db_path).resolve()
            if not db_path.is_file():
                raise RouteConfigurationError(f"canonical database is missing: {db_path}")
            row["canonical_db"] = db_path
            parsed.append(GatewayRoute.model_validate(row))
        registry = cls(parsed)
        registry._project_root = root
        return registry

    @property
    def routes(self) -> tuple[GatewayRoute, ...]:
        return tuple(self._by_id.values())

    def resolve(self, *, workspace_id: str, domain_profile: str) -> GatewayRoute:
        try:
            return self._by_scope[(workspace_id, domain_profile)]
        except KeyError as exc:
            raise RouteNotFoundError(
                f"no route for workspace={workspace_id!r}, domain_profile={domain_profile!r}"
            ) from exc

    def resolve_search(self, token: AgentContextToken, request: SearchRequest) -> GatewayRoute:
        if len(request.domains) != 1:
            raise RouteConfigurationError(
                "routed search requires exactly one explicit domain_profile"
            )
        route = self.resolve(workspace_id=token.workspace_id, domain_profile=request.domains[0])
        if RouteCapability.QUERY not in route.capabilities:
            raise RouteCapabilityError(f"route {route.route_id} does not allow query")
        return route

    def validate_scope(self, route: GatewayRoute, scope: AccessScope) -> None:
        projects = set(scope.project_ids)
        if route.visibility_policy.require_project_scope and not projects:
            raise RouteVisibilityError("route requires an explicit project scope")
        escaped = projects - set(route.allowed_projects)
        if escaped:
            raise RouteVisibilityError(
                f"project scope escapes route {route.route_id}: {sorted(escaped)}"
            )
        if scope.allow_global_kb and not route.visibility_policy.allow_global_kb:
            raise RouteVisibilityError("global KB access is forbidden for this route")
        if not set(route.visibility_policy.allowed_visibilities):
            raise RouteVisibilityError("route has no allowed visibility values")

    @staticmethod
    def validate_alias_targets(route: GatewayRoute, vector_index: Any) -> dict[str, str | None]:
        aliases = vector_index.aliases()
        alias_names = [route.read_alias]
        if route.write_alias is not None:
            alias_names.append(route.write_alias)
        targets = {alias: aliases.get(alias) for alias in alias_names}
        for alias, target in targets.items():
            if target is None:
                raise RouteAliasError(f"route {route.route_id} alias is missing: {alias}")
            if target != route.physical_collection:
                raise RouteAliasError(
                    f"route {route.route_id} alias {alias} points to {target}, expected {route.physical_collection}"
                )
        return targets

    @staticmethod
    def production_aliases_present(route: GatewayRoute, vector_index: Any) -> list[str]:
        client = getattr(vector_index, "client", None)
        if client is not None:
            raw_aliases = client.list_aliases()
            aliases = set(raw_aliases.get("aliases", [])) if isinstance(raw_aliases, dict) else set(raw_aliases)
        else:
            aliases = set(vector_index.aliases())
        return [
            alias for alias in (route.production_read_alias_name, route.production_write_alias_name)
            if alias is not None and alias in aliases
        ]


class RoutedHybridSearchGateway:
    def __init__(
        self,
        *,
        registry: GatewayRouteRegistry,
        gateway_factory: Callable[[GatewayRoute], tuple[HybridSearchGateway, Any]],
    ) -> None:
        self.registry = registry
        self.gateway_factory = gateway_factory
        self._cache: dict[str, tuple[HybridSearchGateway, Any]] = {}

    def _components(self, route: GatewayRoute):
        cached = self._cache.get(route.route_id)
        if cached is None:
            cached = self.gateway_factory(route)
            self._cache[route.route_id] = cached
        return cached

    def retrieve_context(self, token: AgentContextToken, request: SearchRequest, *, now=None):
        route = self.registry.resolve_search(token, request)
        gateway, token_validator = self._components(route)
        scope = token_validator.authorize_search(token, request, profiles=gateway.profiles, now=now)
        self.registry.validate_scope(route, scope)
        return gateway.retrieve_context(token, request, now=now, preauthorized_scope=scope)

    def search(self, token: AgentContextToken, request: SearchRequest, *, now=None):
        route = self.registry.resolve_search(token, request)
        gateway, token_validator = self._components(route)
        scope = token_validator.authorize_search(token, request, profiles=gateway.profiles, now=now)
        self.registry.validate_scope(route, scope)
        return gateway.search(token, request, now=now, preauthorized_scope=scope)


class RoutedMemoryWriter:
    def __init__(
        self,
        *,
        registry: GatewayRouteRegistry,
        writer_factory: Callable[[GatewayRoute], Any],
    ) -> None:
        self.registry = registry
        self.writer_factory = writer_factory
        self._cache: dict[str, Any] = {}

    def write(self, item: MemoryWriteInput, token: AgentContextToken):
        route = self.registry.resolve(workspace_id=item.workspace_id, domain_profile=item.domain_profile)
        if RouteCapability.WRITE not in route.capabilities:
            raise RouteCapabilityError(f"route {route.route_id} does not allow write")
        if token.workspace_id != route.workspace_id:
            raise RouteVisibilityError("token workspace does not match selected route")
        if item.project_id not in route.allowed_projects:
            raise RouteVisibilityError("item project is outside the selected route")
        writer = self._cache.get(route.route_id)
        if writer is None:
            writer = self.writer_factory(route)
            self._cache[route.route_id] = writer
        return writer.write(item, token)


def load_default_registry(project_root: str | Path | None = None) -> GatewayRouteRegistry:
    root = Path(project_root).resolve() if project_root is not None else Path(__file__).resolve().parents[2]
    return GatewayRouteRegistry.load(root / "config" / "gateway_routes.json", project_root=root)
