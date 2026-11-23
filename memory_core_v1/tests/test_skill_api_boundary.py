from __future__ import annotations

import ast
from pathlib import Path

from memory_core.readonly_milvus import ReadOnlyMilvusIndex


ROOT = Path(__file__).resolve().parents[1]
SKILL_API_ROOT = ROOT / "src" / "memory_core" / "skill_api"


def test_skill_api_does_not_import_one_off_pipeline_modules() -> None:
    forbidden_roots = ("memory_core.step", "memory_core.scripts", "memory_core.answer")
    violations: list[str] = []
    for path in SKILL_API_ROOT.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                modules = [node.module] if node.module else []
            else:
                continue
            for module in modules:
                if module and module.startswith(forbidden_roots):
                    violations.append(f"{path.name}:{node.lineno}:{module}")
    assert violations == []


def test_readonly_milvus_has_no_write_api() -> None:
    for name in (
        "upsert", "delete", "bind_aliases", "switch_read_alias",
        "ensure_collection", "create_probe_collection",
    ):
        assert not hasattr(ReadOnlyMilvusIndex, name)