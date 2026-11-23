from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from ..config import EmbeddingSettings
from ..embeddings import OpenAICompatibleEmbeddingClient
from ..pymilvus_index import PymilvusMemoryIndex
from ..readonly_milvus import ReadOnlyMilvusIndex
from .models import DatabaseProfile, SkillApiBlocked, SkillBackend


def create_readonly_milvus_from_profile(
    profile: DatabaseProfile,
    *,
    uri_env: str = "MILVUS_URI",
) -> ReadOnlyMilvusIndex:
    if profile.backend != SkillBackend.MILVUS:
        raise SkillApiBlocked("profile backend is not milvus")
    if not profile.read_alias:
        raise SkillApiBlocked("profile does not contain a read alias")
    if not profile.embedding_dimension:
        raise SkillApiBlocked("profile does not contain an embedding dimension")
    uri = os.environ.get(uri_env)
    if not uri:
        raise SkillApiBlocked(f"environment variable is not configured: {uri_env}")
    try:
        return ReadOnlyMilvusIndex(
            uri=uri,
            read_alias=profile.read_alias,
            dimension=profile.embedding_dimension,
            metric_type=profile.metric_type or "COSINE",
        )
    except RuntimeError as exc:
        raise SkillApiBlocked("read-only Milvus initialization failed") from exc


def create_default_embedding_client() -> Any:
    settings = EmbeddingSettings.from_env()
    if not settings.api_key:
        raise SkillApiBlocked("EMBEDDING_API_KEY is not configured")
    return OpenAICompatibleEmbeddingClient(settings)
def create_bootstrap_milvus_index(plan: Any, *, uri_env: str = "MILVUS_URI") -> PymilvusMemoryIndex:
    uri = os.environ.get(uri_env)
    if not uri:
        raise SkillApiBlocked(f"environment variable is not configured: {uri_env}")
    if "prod" in (plan.read_alias + plan.write_alias).casefold():
        raise SkillApiBlocked("production aliases are forbidden in bootstrap")
    try:
        return PymilvusMemoryIndex(
            uri=uri,
            collection_name=plan.physical_collection,
            read_alias=plan.read_alias,
            write_alias=plan.write_alias,
            dimension=plan.embedding_dimension,
        )
    except RuntimeError as exc:
        raise SkillApiBlocked("bootstrap Milvus initialization failed") from exc
class MilvusBootstrapProbe:
    def __init__(self, *, client: Any, collection_name: str) -> None:
        self._client = client
        self._collection_name = collection_name

    def has_collection(self, name: str | None = None) -> bool:
        return bool(self._client.has_collection(name or self._collection_name))

    def list_aliases(self) -> Any:
        return self._client.list_aliases()


def create_bootstrap_resource_probe(
    *, route_path: str | Path, workspace_id: str, domain_profile: str, uri_env: str = "MILVUS_URI",
) -> MilvusBootstrapProbe:
    from ..routing import GatewayRouteRegistry
    from pymilvus import MilvusClient
    route = GatewayRouteRegistry.load(route_path).resolve(
        workspace_id=workspace_id,
        domain_profile=domain_profile,
    )
    uri = os.environ.get(uri_env)
    if not uri:
        raise SkillApiBlocked(f"environment variable is not configured: {uri_env}")
    return MilvusBootstrapProbe(
        client=MilvusClient(uri=uri),
        collection_name=route.physical_collection,
    )