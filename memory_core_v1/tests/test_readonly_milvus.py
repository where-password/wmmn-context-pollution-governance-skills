from __future__ import annotations

from pathlib import Path

import pytest

from memory_core.readonly_milvus import ReadOnlyMilvusIndex


READ_ALIAS = "memory_read_alias_attach_test_001"
COLLECTION = "memory_core_attach_test_v1"


class FakeIterator:
    def __init__(self, batches):
        self._batches = iter(batches)
        self.closed = False

    def next(self):
        return next(self._batches, [])

    def close(self):
        self.closed = True


class FakeMilvusClient:
    def __init__(self):
        self.closed_iterator: FakeIterator | None = None

    def list_aliases(self):
        return {"aliases": [READ_ALIAS]}

    def describe_alias(self, *, alias):
        return {"alias": alias, "collection_name": COLLECTION}

    def describe_collection(self, *, collection_name):
        return {"collection_name": collection_name, "fields": ["memory_id", "embedding"]}

    def query(self, *, collection_name, filter, output_fields):
        assert collection_name == READ_ALIAS
        return [{"count(*)": 2}]

    def get_load_state(self, collection_name):
        assert collection_name == READ_ALIAS
        return {"state": 3}

    def search(self, **kwargs):
        return [[{"id": "m1", "distance": 0.9, "entity": {"memory_id": "m1"}}]]

    def query_iterator(self, **kwargs):
        iterator = FakeIterator([[{"memory_id": "m1", "version": 1}], []])
        self.closed_iterator = iterator
        return iterator


def test_readonly_index_exposes_only_read_surface() -> None:
    client = FakeMilvusClient()
    index = ReadOnlyMilvusIndex(read_alias=READ_ALIAS, dimension=1024, client=client)

    assert index.aliases() == {READ_ALIAS: COLLECTION}
    assert index.describe_collection()["collection_name"] == COLLECTION
    assert index.count() == 2
    assert index.load_state() == {"state": 3}
    assert index.search([0.0] * 1024)[0]["memory_id"] == "m1"
    assert list(index.iter_rows(batch_size=1)) == [{"memory_id": "m1", "version": 1}]
    assert client.closed_iterator is not None and client.closed_iterator.closed
    for forbidden in (
        "upsert", "delete", "bind_aliases", "switch_read_alias",
        "ensure_collection", "create_probe_collection",
    ):
        assert not hasattr(index, forbidden)


def test_readonly_index_rejects_wrong_query_dimension() -> None:
    index = ReadOnlyMilvusIndex(read_alias=READ_ALIAS, dimension=1024, client=FakeMilvusClient())
    with pytest.raises(ValueError, match="dimension"):
        index.search([0.0])