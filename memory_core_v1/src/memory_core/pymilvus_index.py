from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from .models import MemoryIndexDoc


class MilvusIndexError(RuntimeError):
    pass


class PymilvusMemoryIndex:
    def __init__(
        self,
        *,
        uri: str,
        collection_name: str | None = None,
        read_alias: str,
        write_alias: str,
        dimension: int = 1024,
        metric_type: str = "COSINE",
    ) -> None:
        try:
            from pymilvus import DataType, MilvusClient
        except ImportError as exc:
            raise MilvusIndexError("pymilvus is not installed") from exc
        self.DataType = DataType
        self.client = MilvusClient(uri=uri)
        self.uri = uri
        self.collection_name = collection_name
        self.read_alias = read_alias
        self.write_alias = write_alias
        self.dimension = dimension
        self.metric_type = metric_type

    def ensure_collection(self) -> bool:
        if not self.collection_name:
            raise MilvusIndexError("physical collection name is required for collection management")
        if self.client.has_collection(self.collection_name):
            return False
        schema = self.client.create_schema(auto_id=False, enable_dynamic_field=False)
        schema.add_field("memory_id", self.DataType.VARCHAR, is_primary=True, max_length=64)
        schema.add_field("embedding", self.DataType.FLOAT_VECTOR, dim=self.dimension)
        varchar_fields = (
            ("workspace_id", 128), ("project_id", 128), ("task_id", 128),
            ("conversation_id", 128), ("owner_principal_id", 128),
            ("domain_profile", 64), ("profile_version", 32), ("memory_type", 64),
            ("subtype", 64), ("subject", 512), ("normalized_subject", 512),
            ("status", 32), ("importance", 16), ("sensitivity", 16),
            ("visibility", 16), ("embedding_model", 128), ("normalization", 16),
            ("index_schema_version", 64), ("collection_alias", 128),
            ("canonical_text", 8192), ("embedding_text", 8192),
        )
        for name, length in varchar_fields:
            schema.add_field(name, self.DataType.VARCHAR, max_length=length)
        schema.add_field("effective_from_ts", self.DataType.INT64)
        schema.add_field("effective_to_ts", self.DataType.INT64)
        schema.add_field("version", self.DataType.INT64)
        schema.add_field("embedding_dim", self.DataType.INT64)
        schema.add_field("source_scope_json", self.DataType.JSON)
        schema.add_field("indexed_payload_json", self.DataType.JSON)
        schema.add_field("created_at_ts", self.DataType.INT64)
        index_params = self.client.prepare_index_params()
        index_params.add_index(
            field_name="embedding", index_type="AUTOINDEX", metric_type=self.metric_type
        )
        for field in (
            "workspace_id", "project_id", "owner_principal_id", "domain_profile",
            "memory_type", "status", "sensitivity", "visibility", "normalized_subject",
        ):
            index_params.add_index(field_name=field, index_type="INVERTED")
        self.client.create_collection(
            collection_name=self.collection_name,
            schema=schema,
            index_params=index_params,
        )
        self.client.load_collection(self.collection_name)
        return True

    @staticmethod
    def _doc_dict(doc: MemoryIndexDoc) -> dict[str, Any]:
        return doc.model_dump(mode="json")

    def _aliases(self, collection_name: str = "") -> list[str]:
        result = self.client.list_aliases(collection_name=collection_name)
        if isinstance(result, dict):
            return list(result.get("aliases", []))
        return list(result)

    def _alias_target(self, alias: str) -> str | None:
        if alias not in self._aliases():
            return None
        result = self.client.describe_alias(alias=alias)
        if isinstance(result, dict):
            return result.get("collection_name") or result.get("collection")
        return None

    def bind_aliases(self, *, switch_if_exists: bool = False) -> None:
        if not self.collection_name:
            raise MilvusIndexError("physical collection name is required for alias binding")
        aliases = set(self._aliases())
        for alias in (self.read_alias, self.write_alias):
            if alias in aliases:
                target = self._alias_target(alias)
                if target != self.collection_name:
                    if not switch_if_exists:
                        raise MilvusIndexError(
                            f"alias {alias} points to {target}, refusing implicit switch"
                        )
                    self.client.alter_alias(collection_name=self.collection_name, alias=alias)
            else:
                self.client.create_alias(collection_name=self.collection_name, alias=alias)

    def upsert(
        self, docs: Sequence[MemoryIndexDoc], *, target: str | None = None,
        allow_physical: bool = False,
    ) -> int:
        target = target or self.write_alias
        if not allow_physical and target not in {self.read_alias, self.write_alias}:
            raise PermissionError("business writes may only use memory_write_alias")
        if not docs:
            return 0
        data = [self._doc_dict(doc) for doc in docs]
        self.client.upsert(collection_name=target, data=data)
        return len(data)

    def delete(
        self,
        memory_ids: Sequence[str],
        *,
        target: str | None = None,
        allow_physical: bool = False,
    ) -> int:
        target = target or self.write_alias
        if not allow_physical and target not in {self.read_alias, self.write_alias}:
            raise PermissionError("business deletes may only use memory_write_alias")
        if not memory_ids:
            return 0
        self.client.delete(collection_name=target, ids=list(memory_ids))
        return len(memory_ids)

    def search_ids(self, embedding: list[float], *, filter: str = "", limit: int = 100) -> list[tuple[str, float]]:
        if len(embedding) != self.dimension:
            raise ValueError(f"query embedding dimension must be {self.dimension}")
        result = self.client.search(
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

    def search(
        self, embedding: list[float], *, filter: str = "", limit: int = 20,
    ) -> list[dict[str, Any]]:
        if len(embedding) != self.dimension:
            raise ValueError(f"query embedding dimension must be {self.dimension}")
        result = self.client.search(
            collection_name=self.read_alias,
            data=[embedding],
            filter=filter,
            limit=limit,
            output_fields=[
                "memory_id", "workspace_id", "project_id", "owner_principal_id",
                "domain_profile", "memory_type", "subtype", "subject",
                "normalized_subject", "status", "importance", "sensitivity",
                "visibility", "version", "canonical_text", "source_scope_json",
                "indexed_payload_json",
            ],
            search_params={"metric_type": self.metric_type},
        )
        if not result:
            return []
        hits = []
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
    ) -> Any:
        """Iterate every matching entity without the 1,000-row query ceiling."""
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        fields = output_fields or [
            "memory_id", "workspace_id", "project_id", "domain_profile",
            "memory_type", "status", "sensitivity", "visibility", "version",
            "index_schema_version", "embedding_model", "embedding_dim",
            "canonical_text",
        ]
        iterator = self.client.query_iterator(
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

    def query(self, *, expression: str = "", output_fields: list[str] | None = None, limit: int = 1000) -> list[dict[str, Any]]:
        fields = output_fields or [
            "memory_id", "workspace_id", "project_id", "owner_principal_id",
            "domain_profile", "memory_type", "subtype", "status", "sensitivity",
            "visibility", "version", "canonical_text",
        ]
        return list(self.client.query(
            collection_name=self.read_alias,
            filter=expression,
            output_fields=fields,
            limit=limit,
        ))

    def count(self, *, filter: str = "") -> int:
        result = self.client.query(
            collection_name=self.read_alias,
            filter=filter,
            output_fields=["count(*)"],
        )
        if not result:
            return 0
        return int(next(iter(result[0].values())))

    def load_state(self) -> dict[str, Any]:
        return dict(self.client.get_load_state(self.read_alias))

    def aliases(self) -> dict[str, str | None]:
        return {alias: self._alias_target(alias) for alias in (self.read_alias, self.write_alias)}

    def switch_read_alias(self, new_collection: str, *, expected_current: str | None = None) -> str | None:
        current = self._alias_target(self.read_alias)
        if expected_current is not None and current != expected_current:
            raise MilvusIndexError(f"read alias drift: expected {expected_current}, actual {current}")
        self.client.alter_alias(collection_name=new_collection, alias=self.read_alias)
        return current

    def create_probe_collection(self, collection_name: str) -> bool:
        original = self.collection_name
        try:
            self.collection_name = collection_name
            return self.ensure_collection()
        finally:
            self.collection_name = original
