from __future__ import annotations

import socket
from pathlib import Path

import pytest

from memory_core.pymilvus_index import PymilvusMemoryIndex


ROOT = Path(__file__).resolve().parents[1]
READ_ALIAS = "memory_read_alias_test_001"
WRITE_ALIAS = "memory_write_alias_test_001"


def milvus_available() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", 19530), timeout=1):
            return True
    except OSError:
        return False


def test_business_query_files_do_not_hardcode_physical_collection() -> None:
    files = [
        ROOT / "src" / "memory_core" / "search.py",
    ]
    for path in files:
        text = path.read_text(encoding="utf-8")
        assert "memory_core_stress105_v5_v1" not in text
        assert "memory_core_stress105_v5_alias_probe" not in text


@pytest.mark.skipif(not milvus_available(), reason="Milvus test instance unavailable")
def test_actual_alias_targets_and_count_are_consistent() -> None:
    index = PymilvusMemoryIndex(
        uri="http://127.0.0.1:19530", read_alias=READ_ALIAS,
        write_alias=WRITE_ALIAS, dimension=1024,
    )
    aliases = index.aliases()
    assert aliases[READ_ALIAS] is not None
    assert aliases[WRITE_ALIAS] == aliases[READ_ALIAS]
    assert index.count() == 1254
    assert index.load_state().get("state") == 3


@pytest.mark.skipif(not milvus_available(), reason="Milvus test instance unavailable")
def test_query_iterator_returns_all_1254_rows_without_thousand_row_cap() -> None:
    index = PymilvusMemoryIndex(
        uri="http://127.0.0.1:19530", read_alias=READ_ALIAS,
        write_alias=WRITE_ALIAS, dimension=1024,
    )
    rows = list(index.iter_rows(batch_size=137, output_fields=["memory_id", "version"]))
    assert len(rows) == 1254
    assert len({row["memory_id"] for row in rows}) == 1254
