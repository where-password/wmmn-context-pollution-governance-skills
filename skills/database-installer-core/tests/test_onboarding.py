from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "onboarding.py"


def test_onboarding_is_shown_once_per_codex_home(tmp_path: Path) -> None:
    def show() -> dict:
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--codex-home", str(tmp_path / "codex-home"), "--json"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
        )
        return json.loads(result.stdout)

    first = show()
    second = show()
    assert first["shown"] is True
    assert "对话清理" in first["message"]
    assert "数据库 Skill" in first["message"]
    assert "wmmn · 我密码呢" in first["message"]
    assert first["watermark"] == {"mark": "wmmn", "author": "我密码呢"}
    assert second["shown"] is False
    assert second["message"] is None
