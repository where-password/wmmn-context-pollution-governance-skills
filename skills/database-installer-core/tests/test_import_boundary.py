from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
MEMORY_CORE_SRC = ROOT.parents[1] / "memory_core_v1" / "src"


def test_installer_ast_imports_only_skill_api() -> None:
    violations: list[str] = []
    for path in SCRIPTS.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules = [node.module]
            else:
                continue
            for module in modules:
                if module == "memory_core" or module.startswith("memory_core."):
                    if module != "memory_core.skill_api":
                        violations.append(f"{path.name}:{node.lineno}:{module}")
    assert violations == []


def test_runtime_dependency_graph_does_not_load_internal_pipeline_modules() -> None:
    code = r'''
import importlib.abc
import sys

FORBIDDEN = (
    "memory_core.step3_candidates",
    "memory_core.step4_groups",
    "memory_core.step4_review",
    "memory_core.step4b1a",
    "memory_core.step4b1b",
    "memory_core.step5a",
    "memory_core.step5b",
    "memory_core.step5c",
    "memory_core.step6a",
    "memory_core.step7a",
    "memory_core.step7c",
    "memory_core.step7d",
)

class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in FORBIDDEN:
            raise ImportError(f"forbidden internal import: {fullname}")
        return None

sys.meta_path.insert(0, Block())
import db_skill
loaded = sorted(name for name in sys.modules if name in FORBIDDEN)
print(loaded)
raise SystemExit(0 if not loaded else 1)
'''
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join([str(SCRIPTS), str(MEMORY_CORE_SRC)])
    result = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr + result.stdout