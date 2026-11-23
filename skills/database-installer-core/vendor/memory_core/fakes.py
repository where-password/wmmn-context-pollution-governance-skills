from __future__ import annotations

import hashlib
import math
from collections.abc import Sequence
from typing import Any

from .models import MemoryIndexDoc


class FakeEmbedding:
    def __init__(self, *, dimension: int = 1024, model: str = "fake-bge-m3-v1") -> None:
        self.dimension = dimension
        self.model = model
        self.embedding_version = model
        self.calls = 0

    def embed_one(self, text: str) -> list[float]:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        values: list[float] = []
        for index in range(self.dimension):
            byte = digest[index % len(digest)]
            mixed = (byte ^ ((index * 37) & 0xFF)) / 255.0
            values.append(mixed * 2.0 - 1.0)
        norm = math.sqrt(sum(value * value for value in values)) or 1.0
        return [value / norm for value in values]

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls += 1
        return [self.embed_one(text) for text in texts]


class FakeVectorIndex:
    def __init__(
        self,
        *,
        read_alias: str = "memory_read_alias",
        write_alias: str = "memory_write_alias",
        fail_remaining: int = 0,
    ) -> None:
        self.read_alias = read_alias
        self.write_alias = write_alias
        self.fail_remaining = fail_remaining
        self.collections: dict[str, dict[str, MemoryIndexDoc]] = {
            read_alias: {},
            write_alias: {},
        }
        self.upsert_calls = 0
        self.delete_calls = 0

    def _check_failure(self) -> None:
        if self.fail_remaining > 0:
            self.fail_remaining -= 1
            raise RuntimeError("fake vector index unavailable")

    def ensure_collection(self, name: str) -> None:
        self.collections.setdefault(name, {})

    def upsert(self, docs: Sequence[MemoryIndexDoc], *, target: str | None = None, allow_physical: bool = False) -> int:
        self._check_failure()
        target = target or self.write_alias
        if not allow_physical and target not in {self.read_alias, self.write_alias}:
            raise PermissionError("business writes may only use memory_write_alias")
        self.ensure_collection(target)
        self.upsert_calls += 1
        for doc in docs:
            self.collections[target][doc.memory_id] = doc
        if target == self.write_alias:
            self.collections[self.read_alias].update({doc.memory_id: doc for doc in docs})
        return len(docs)

    def delete(self, memory_ids: Sequence[str], *, target: str | None = None, allow_physical: bool = False) -> int:
        self._check_failure()
        target = target or self.write_alias
        if not allow_physical and target not in {self.read_alias, self.write_alias}:
            raise PermissionError("business deletes may only use memory_write_alias")
        removed = 0
        for memory_id in memory_ids:
            for name in {target} | ({self.read_alias, self.write_alias} if target == self.write_alias else set()):
                self.ensure_collection(name)
                if name in self.collections and self.collections[name].pop(memory_id, None) is not None:
                    removed += 1
        self.delete_calls += 1
        return removed

    def clear(self, *, target: str | None = None, allow_physical: bool = False) -> None:
        target = target or self.write_alias
        if not allow_physical and target not in {self.read_alias, self.write_alias}:
            raise PermissionError("business clears may only use memory_write_alias")
        self.collections[target] = {}
        if target == self.write_alias:
            self.collections[self.read_alias] = {}

    @staticmethod
    def _cosine(left: list[float], right: list[float]) -> float:
        dot = sum(a * b for a, b in zip(left, right))
        left_norm = math.sqrt(sum(a * a for a in left)) or 1.0
        right_norm = math.sqrt(sum(b * b for b in right)) or 1.0
        return dot / (left_norm * right_norm)

    def search(
        self,
        embedding: list[float],
        *,
        limit: int = 20,
        predicate=None,
        target: str | None = None,
    ) -> list[tuple[MemoryIndexDoc, float]]:
        self._check_failure()
        target = target or self.read_alias
        if target != self.read_alias:
            raise PermissionError("business search must use memory_read_alias")
        docs = self.collections.get(target, {})
        scored = []
        for doc in docs.values():
            if predicate is not None and not predicate(doc):
                continue
            scored.append((doc, self._cosine(embedding, doc.embedding)))
        scored.sort(key=lambda item: (-item[1], item[0].memory_id))
        return scored[:limit]

    def iter_rows(self, *, filter: str = "", batch_size: int = 1000, output_fields: list[str] | None = None):
        del filter, output_fields
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        docs = list(self.collections.get(self.read_alias, {}).values())
        for start in range(0, len(docs), batch_size):
            for doc in docs[start:start + batch_size]:
                yield doc.model_dump(mode="json")

    def count(self, target: str | None = None, *, filter: str = "") -> int:
        del filter
        return len(self.collections.get(target or self.read_alias, {}))
