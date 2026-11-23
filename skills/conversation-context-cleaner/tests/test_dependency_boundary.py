from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from conversation_cleaner import ConversationSkillApi, CONVERSATION_SKILL_API_VERSION


FORBIDDEN_MODULE_ROOTS = {
    "chat_etl",
    "dotenv",
    "http",
    "httpx",
    "memory_core",
    "openai",
    "psycopg",
    "pymilvus",
    "redis",
    "requests",
    "socket",
    "sqlalchemy",
    "sqlite3",
    "subprocess",
    "urllib",
}


class DependencyBoundaryTests(unittest.TestCase):
    def test_package_has_no_database_network_or_process_imports(self) -> None:
        violations: list[str] = []
        for path in sorted((SCRIPTS / "conversation_cleaner").glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    modules = [node.module or ""]
                else:
                    continue
                for module in modules:
                    if module.split(".", 1)[0] in FORBIDDEN_MODULE_ROOTS:
                        violations.append(f"{path.name}:{node.lineno}:{module}")
        self.assertEqual(violations, [])

    def test_versioned_api_surface_is_explicit(self) -> None:
        self.assertEqual(CONVERSATION_SKILL_API_VERSION, "1.5.0")
        self.assertEqual(
            set(ConversationSkillApi),
            {"doctor", "clean_plan", "clean_apply", "status", "review_export"},
        )
        self.assertTrue(all(callable(value) for value in ConversationSkillApi.values()))


    def test_merge_rules_do_not_hardcode_pilot_business_terms(self) -> None:
        source = "\n".join(
            path.read_text(encoding="utf-8").casefold()
            for path in (
                SCRIPTS / "conversation_cleaner" / "extraction.py",
                SCRIPTS / "conversation_cleaner" / "consolidation.py",
            )
        )
        for term in ("database_skill", "profile attach", "src/memory_core", "旧脚本", "secret_guard", "readonlyroute"):
            self.assertNotIn(term.casefold(), source)


if __name__ == "__main__":
    unittest.main()
