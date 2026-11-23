from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ReadonlyMemoryItem(_Strict):
    memory_id: str
    chapter_index: int | None = None
    source_local_order: int | None = None
    memory_type: str
    subtype: str | None = None
    memory_category: str | None = None
    importance: str
    content: str
    retention_reason: str | None = None
    story_function: str | None = None
    source_scope_json: dict[str, Any]


class ReadonlyMemoryPage(_Strict):
    total: int
    offset: int
    page_size: int
    items: list[ReadonlyMemoryItem]
    next_offset: int | None = None


def list_memory_page(
    db_path: str | Path, *,
    memory_types: list[str] | None = None,
    memory_categories: list[str] | None = None,
    chapter_start: int | None = None,
    chapter_end: int | None = None,
    offset: int = 0,
    page_size: int = 50,
) -> ReadonlyMemoryPage:
    if offset < 0:
        raise ValueError("offset must be non-negative")
    if page_size < 1 or page_size > 200:
        raise ValueError("page_size must be in [1, 200]")
    if chapter_start is not None and chapter_end is not None and chapter_end < chapter_start:
        raise ValueError("chapter_end must not precede chapter_start")
    path = Path(db_path).resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    conditions = ["status='active'", "deleted_at IS NULL"]
    params: list[Any] = []
    if memory_types:
        placeholders = ",".join("?" for _ in memory_types)
        conditions.append(f"memory_type IN ({placeholders})")
        params.extend(memory_types)
    if memory_categories:
        placeholders = ",".join("?" for _ in memory_categories)
        conditions.append(f"json_extract(domain_payload_json,'$.memory_category') IN ({placeholders})")
        params.extend(memory_categories)
    if chapter_start is not None:
        conditions.append("CAST(json_extract(domain_payload_json,'$.chapter_index') AS INTEGER) >= ?")
        params.append(chapter_start)
    if chapter_end is not None:
        conditions.append("CAST(json_extract(domain_payload_json,'$.chapter_index') AS INTEGER) <= ?")
        params.append(chapter_end)
    where = " AND ".join(conditions)
    uri = f"file:{path.as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        connection.row_factory = sqlite3.Row
        total = connection.execute(f"SELECT count(*) FROM memory_items WHERE {where}", params).fetchone()[0]
        rows = connection.execute(
            f"""SELECT memory_id,memory_type,subtype,importance,content,domain_payload_json,
                       source_scope_json
                FROM memory_items WHERE {where}
                ORDER BY CAST(json_extract(domain_payload_json,'$.chapter_index') AS INTEGER),
                         CAST(json_extract(source_scope_json,'$.locator_json.source_local_order') AS INTEGER),
                         memory_id
                LIMIT ? OFFSET ?""",
            [*params, page_size, offset],
        ).fetchall()
    items: list[ReadonlyMemoryItem] = []
    for row in rows:
        payload = json.loads(row["domain_payload_json"])
        scope = json.loads(row["source_scope_json"])
        items.append(ReadonlyMemoryItem(
            memory_id=row["memory_id"],
            chapter_index=payload.get("chapter_index"),
            source_local_order=(scope.get("locator_json") or {}).get("source_local_order"),
            memory_type=row["memory_type"], subtype=row["subtype"],
            memory_category=payload.get("memory_category"), importance=row["importance"],
            content=row["content"], retention_reason=payload.get("retention_reason"),
            story_function=payload.get("story_function"), source_scope_json=scope,
        ))
    next_offset = offset + len(items)
    return ReadonlyMemoryPage(
        total=int(total), offset=offset, page_size=page_size, items=items,
        next_offset=next_offset if next_offset < total else None,
    )
