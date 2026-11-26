from __future__ import annotations

import re
import sqlite3
import urllib.parse
from pathlib import Path
from typing import Any

from memory_core.skill_api import SkillApiBlocked


def _terms(query: str) -> list[str]:
    terms: list[str] = []
    seen: set[str] = set()
    for match in re.finditer(r"[A-Za-z0-9][A-Za-z0-9_.-]{1,}", query):
        term = match.group(0)
        if term.casefold() not in seen:
            seen.add(term.casefold())
            terms.append(term)
    for match in re.finditer(r"[㐀-鿿]{2,}", query):
        chunk = match.group(0)
        for size in range(6, 1, -1):
            for start in range(0, len(chunk) - size + 1):
                term = chunk[start:start + size]
                if term not in seen:
                    seen.add(term)
                    terms.append(term)
                if len(terms) >= 24:
                    return terms
    return terms


def _open_immutable(path: Path) -> sqlite3.Connection:
    if not path.is_file() or path.is_symlink():
        raise SkillApiBlocked("knowledge source database does not exist or is a symlink")
    uri_path = urllib.parse.quote(path.as_posix(), safe="/:")
    connection = sqlite3.connect(f"file:{uri_path}?mode=ro&immutable=1", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=30000")
    return connection


def _clip(value: Any, limit: int) -> str:
    text = "" if value is None else str(value)
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def query_sqlite_knowledge(
    db_path: str | Path,
    *,
    query: str,
    search_query: str | None = None,
    top_k: int = 10,
    max_content_chars: int = 1200,
) -> dict[str, Any]:
    path = Path(db_path).expanduser().resolve()
    if not isinstance(query, str) or not query.strip():
        raise SkillApiBlocked("knowledge query must not be empty")
    if not 1 <= top_k <= 50:
        raise SkillApiBlocked("top_k must be between 1 and 50")
    connection = _open_immutable(path)
    hits: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    warnings: list[str] = []
    try:
        fts_query = (search_query or query).strip()
        try:
            for row in connection.execute(
                """SELECT e.id,e.title,e.summary,e.risk_level,e.tier,e.url,e.categories,
                          bm25(episodes_fts) AS rank_score
                   FROM episodes_fts JOIN episodes e ON e.id=episodes_fts.rowid
                   WHERE episodes_fts MATCH ? ORDER BY rank LIMIT ?""",
                (fts_query, top_k),
            ):
                key = ("episode", int(row["id"]))
                if key in seen:
                    continue
                seen.add(key)
                hits.append({
                    "kind": "episode",
                    "citation_label": f"课程第{int(row['id']):03d}集",
                    "episode_id": int(row["id"]),
                    "title": row["title"],
                    "content": _clip(row["summary"], max_content_chars),
                    "risk_level": row["risk_level"],
                    "tier": row["tier"],
                    "url": row["url"],
                    "categories": row["categories"],
                })
        except sqlite3.Error as exc:
            warnings.append(f"episode search unavailable: {type(exc).__name__}")

        try:
            for row in connection.execute(
                """SELECT p.id,p.platform,p.rule_type,p.title,p.content,p.source_url,p.confidence,
                          bm25(rules_fts) AS rank_score
                   FROM rules_fts JOIN platform_rules p ON p.id=rules_fts.rowid
                   WHERE rules_fts MATCH ? ORDER BY rank LIMIT ?""",
                (fts_query, max(1, top_k // 3)),
            ):
                key = ("platform_rule", int(row["id"]))
                if key in seen:
                    continue
                seen.add(key)
                hits.append({
                    "kind": "platform_rule",
                    "citation_label": f"平台规则：{row['title']}",
                    "rule_id": int(row["id"]),
                    "title": row["title"],
                    "content": _clip(row["content"], max_content_chars),
                    "platform": row["platform"],
                    "source_url": row["source_url"],
                    "confidence": row["confidence"],
                })
        except sqlite3.Error as exc:
            warnings.append(f"rule search unavailable: {type(exc).__name__}")

        try:
            for row in connection.execute(
                """SELECT s.id,s.episode_id,s.start_sec,s.end_sec,s.text,e.title
                   FROM segments_fts JOIN segments s ON s.id=segments_fts.rowid
                   JOIN episodes e ON e.id=s.episode_id
                   WHERE segments_fts MATCH ? ORDER BY bm25(segments_fts) LIMIT ?""",
                (fts_query, max(1, top_k // 2)),
            ):
                key = ("segment", int(row["id"]))
                if key in seen:
                    continue
                seen.add(key)
                hits.append({
                    "kind": "segment",
                    "citation_label": f"课程第{int(row['episode_id']):03d}集 {int(row['start_sec'])}s",
                    "episode_id": int(row["episode_id"]),
                    "title": row["title"],
                    "start_sec": row["start_sec"],
                    "end_sec": row["end_sec"],
                    "content": _clip(row["text"], max_content_chars),
                })
        except sqlite3.Error as exc:
            warnings.append(f"segment search unavailable: {type(exc).__name__}")

        if not hits:
            terms = _terms(query)
            if terms:
                predicates = " OR ".join(["name LIKE ? OR description LIKE ?" ])
                params: list[str] = []
                for term in terms[:8]:
                    needle = f"%{term}%"
                    params.extend([needle, needle])
                try:
                    for row in connection.execute(
                        f"SELECT id,name,category,risk_level,description FROM tools WHERE {predicates} LIMIT ?",
                        [*params, top_k],
                    ):
                        key = ("tool", int(row["id"]))
                        if key in seen:
                            continue
                        seen.add(key)
                        hits.append({
                            "kind": "tool",
                            "citation_label": f"工具：{row['name']}",
                            "tool_id": int(row["id"]),
                            "title": row["name"],
                            "category": row["category"],
                            "risk_level": row["risk_level"],
                            "content": _clip(row["description"], max_content_chars),
                        })
                except sqlite3.Error as exc:
                    warnings.append(f"tool search unavailable: {type(exc).__name__}")
                try:
                    predicates = " OR ".join(["name LIKE ? OR description LIKE ?"])
                    for row in connection.execute(
                        f"SELECT id,name,category,risk_level,description,defensive_note FROM techniques WHERE {predicates} LIMIT ?",
                        [*params, top_k],
                    ):
                        key = ("technique", int(row["id"]))
                        if key in seen:
                            continue
                        seen.add(key)
                        hits.append({
                            "kind": "technique",
                            "citation_label": f"技术：{row['name']}",
                            "technique_id": int(row["id"]),
                            "title": row["name"],
                            "category": row["category"],
                            "risk_level": row["risk_level"],
                            "content": _clip(row["description"], max_content_chars),
                            "defensive_note": _clip(row["defensive_note"], max_content_chars),
                        })
                except sqlite3.Error as exc:
                    warnings.append(f"technique search unavailable: {type(exc).__name__}")
    finally:
        connection.close()
    return {
        "mode": "source_context",
        "source_type": "sqlite_knowledge",
        "source_path": str(path),
        "query": query,
        "hits": hits[:top_k],
        "evidence_completeness": "partial" if hits else "no_evidence",
        "warnings": warnings,
        "write_performed": False,
    }
