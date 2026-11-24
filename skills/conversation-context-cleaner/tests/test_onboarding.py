from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "onboarding.py"
OTHER_SKILL_SCRIPT = ROOT.parent / "database-installer-core" / "scripts" / "onboarding.py"


def _show(script: Path, codex_home: Path) -> dict:
    result = subprocess.run(
        [sys.executable, str(script), "--codex-home", str(codex_home), "--json"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    return json.loads(result.stdout)


class OnboardingTests(unittest.TestCase):
    def test_message_is_shown_once_per_codex_home(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            codex_home = Path(temp) / "codex-home"
            first = _show(SCRIPT, codex_home)
            second = _show(OTHER_SKILL_SCRIPT, codex_home)

            self.assertTrue(first["shown"])
            self.assertIn("对话清理", first["message"])
            self.assertIn("数据库 Skill", first["message"])
            self.assertFalse(second["shown"])
            self.assertIsNone(second["message"])
            self.assertTrue(Path(first["state_path"]).is_file())
