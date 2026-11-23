from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from .pymilvus_index import MilvusIndexError


class ReadOnlyMilvusIndex:
    """Narrow read-only view over a Milvus read alias.

    This class intentionally exposes no collection-management, alias-management,
    upsert, delete, or reconcile methods. Callers that need writes must use the
    separate writable adapter outside the V1 skill boundary.
    """

    _SEARCH_OUTPUT_FIELDS = [
        "memory_id", "workspace_id", "project_id", "owner_principal_id",
        "domain_profile", "memory_type", "subtype", "subject",
        "normalized_subject", "status", "importance", "sensitivity",
        "visibility", "version", "embedding_model", "embedding_dim",
        "index_schema_version", "canonical_text", "source_scope_json",
        "indexed_payload_json",
    ]
    _ITER_OUTPUT_FIELDS = [
        "memory_id", "workspace_id", "project_id", "domain_profile",
        "memory_type", "status", "sensitivity", "visibility", "version",
        "index_schema_version", "embedding_model", "embedding_dim",
        "canonical_text",
    ]

    def __init__(
        self,
        *,
        read_alias: str,
        uri: str | None = None,
        dimension: int = 1024,
        metric_type: str = "COSINE",
        client: Any | None = None,
    ) -> None:
        if not read_alias.strip():
            raise ValueError("read_alias must not be empty")
        if dimension <= 0:
            raise ValueError("dimension must be positive")
        if client is None:
            if not uri:
                raise ValueError("uri is required when client is not injected")
            try:
                from pymilvus import MilvusClient
            except ImportError as exc:
                raise MilvusIndexError("pymilvus is not installed") from exc
            client = MilvusClient(uri=uri)
        self._client = client
        self.read_alias = read_alias
        self.dimension = dimension
        self.metric_type = metric_type

    def list_aliases(self) -> list[str]:
        result = self._client.list_aliases()
        if isinstance(result, dict):
            return list(result.get("aliases", []))
        return list(result)

    def describe_alias(self, alias: str | None = None) -> dict[str, Any]:
        name = alias or self.read_alias
        result = self._client.describe_alias(alias=name)
        if isinstance(result, dict):
            return dict(result)
        return {"alias": name, "collection_name": str(result)}

    def alias_target(self, alias: str | None = None) -> str | None:
        name = alias or self.read_alias
        if name not in self.list_aliases():
            return None
        described = self.describe_alias(name)
        return described.get("collection_name") or described.get("collection")

    def aliases(self) -> dict[str, str | None]:
        return {self.read_alias: self.alias_target()}

    def describe_collection(self, collection_name: str | None = None) -> dict[str, Any]:
        target = collection_name or self.alias_target() or self.read_alias
        result = self._client.describe_collection(collection_name=target)
        if isinstance(result, dict):
            return dict(result)
        return {"collection_name": target}

    def search_ids(self, embedding: list[float], *, filter: str = "", limit: int = 100) -> list[tuple[str, float]]:
        self._validate_embedding(embedding)
        if limit <= 0:
            raise ValueError("limit must be positive")
        result = self._client.search(
            collection_name=self.read_alias,
            data=[embedding],
            filter=filter,
            limit=limit,
            output_fields=["memory_id"],
            search_params={"metric_type": self.metric_type},
        )
        if not result:
            return []
        values: list[tuple[str, float]] = []
        for item in result[0]:
            entity = item.get("entity", {})
            values.append((str(entity.get("memory_id") or item.get("id")), float(item.get("distance", 0.0))))
        return values

    def search(self, embedding: list[float], *, filter: str = "", limit: int = 20) -> list[dict[str, Any]]:
        self._validate_embedding(embedding)
        if limit <= 0:
            raise ValueError("limit must be positive")
        result = self._client.search(
            collection_name=self.read_alias,
            data=[embedding],
            filter=filter,
            limit=limit,
            output_fields=self._SEARCH_OUTPUT_FIELDS,
            search_params={"metric_type": self.metric_type},
        )
        if not result:
            return []
        hits: list[dict[str, Any]] = []
        for item in result[0]:
            entity = item.get("entity", {})
            hits.append({
                "memory_id": entity.get("memory_id") or item.get("id"),
                "distance": item.get("distance"),
                "entity": entity,
            })
        return hits

    def iter_rows(
        self,
        *,
        filter: str = "",
        batch_size: int = 1000,
        output_fields: list[str] | None = None,
    ) -> Iterator[dict[str, Any]]:
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        fields = output_fields or self._ITER_OUTPUT_FIELDS
        iterator = self._client.query_iterator(
            collection_name=self.read_alias,
            batch_size=batch_size,
            limit=-1,
            filter=filter,
            output_fields=fields,
        )
        try:
            while True:
                batch = iterator.next()
                if not batch:
                    break
                for row in batch:
                    yield dict(row)
        finally:
            close = getattr(iterator, "close", None)
            if close is not None:
                close()

    def count(self, *, filter: str = "") -> int:
        result = self._client.query(
            collection_name=self.read_alias,
            filter=filter,
            output_fields=["count(*)"],
        )
        if not result:
            return 0
        return int(next(iter(result[0].values())))

    def load_state(self) -> dict[str, Any]:
        return dict(self._client.get_load_state(self.read_alias))

    def _validate_embedding(self, embedding: list[float]) -> None:
        if len(embedding) != self.dimension:
            raise ValueError(f"query embedding dimension must be {self.dimension}")