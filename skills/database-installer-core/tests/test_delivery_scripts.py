from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INSTALL_SCRIPT = ROOT / "scripts" / "install_skill.ps1"
UNINSTALL_SCRIPT = ROOT / "scripts" / "uninstall_skill.ps1"
BOOTSTRAP_SCRIPT = ROOT / "scripts" / "bootstrap_runtime.ps1"

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="V1 delivery scripts target Windows")


def _powershell_json(script: Path, *args: str, env: dict | None = None) -> tuple[int, dict]:
    command = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), *args, "-Json"]
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)
    return result.returncode, json.loads(result.stdout)


def test_install_skill_runtime_only_no_overwrite_and_uninstall_preserves_user_data(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex-home"
    target = codex_home / "skills" / "database-installer-core"
    code, _ = _powershell_json(INSTALL_SCRIPT, "-CodexHome", str(codex_home))
    assert code == 0
    assert (target / "SKILL.md").is_file()
    assert (target / "scripts" / "db_skill.py").is_file()
    assert (target / "assets" / "milvus" / "docker-compose.yml").is_file()
    assert (target / "vendor" / "memory_core" / "skill_api" / "installer.py").is_file()
    assert (target / "vendor" / "migrations" / "001_core.sql").is_file()
    assert not (target / "tests").exists()
    assert not (target / "work").exists()

    code, payload = _powershell_json(INSTALL_SCRIPT, "-CodexHome", str(codex_home))
    assert code == 2
    assert payload["status"] == "blocked"
    assert "already exists" in payload["error"]

    keep = target / "workspace" / "keep.txt"
    keep.parent.mkdir(parents=True, exist_ok=True)
    keep.write_text("user-owned", encoding="utf-8")
    code, payload = _powershell_json(UNINSTALL_SCRIPT, "-CodexHome", str(codex_home))
    assert code == 0
    assert payload["status"] == "uninstalled"
    assert keep.is_file()
    assert not (target / "SKILL.md").exists()
    assert not (target / "vendor").exists()


def test_bootstrap_install_python_is_plan_only_without_apply(tmp_path: Path) -> None:
    env = os.environ.copy()
    env["PATH"] = r"C:\Windows\System32"
    env["LOCALAPPDATA"] = str(tmp_path / "no-local-python")
    env["ProgramFiles"] = str(tmp_path / "no-program-files")
    env["ProgramW6432"] = str(tmp_path / "no-program-files")
    env.pop("MEMORY_DB_PYTHON", None)
    root = tmp_path / "workspace"
    command = [
        "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(BOOTSTRAP_SCRIPT),
        "-WorkspaceRoot", str(root), "-InstallPython", "-Json",
    ]
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)
    assert result.returncode == 2
    payload = json.loads(result.stdout)
    assert payload["actions"][0]["action_id"] == "install_supported_python"
    assert "winget" in payload["actions"][0]["command"]
    assert not root.exists()


def test_bootstrap_dependency_repair_plan_does_not_create_venv(tmp_path: Path) -> None:
    python = Path(sys.executable)
    root = tmp_path / "workspace"
    code, payload = _powershell_json(
        BOOTSTRAP_SCRIPT,
        "-WorkspaceRoot", str(root),
        "-PythonExe", str(python),
        "-MemoryCoreSource", str(ROOT.parents[1] / "memory_core_v1"),
    )
    assert code == 0
    action_ids = {action["action_id"] for action in payload["actions"]}
    assert "create_isolated_runtime" in action_ids
    assert "install_locked_dependencies" in action_ids
    assert not (root / ".runtime").exists()


def test_memory_db_cmd_forwards_milvus_help(tmp_path: Path) -> None:
    env = os.environ.copy()
    env["MEMORY_DB_PYTHON"] = sys.executable
    env["MEMORY_CORE_SRC"] = str(ROOT.parents[1] / "memory_core_v1" / "src")
    command = [str(ROOT / "scripts" / "memory-db.cmd"), "milvus", "--help"]
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)
    assert result.returncode == 0
    assert "status" in result.stdout and "bootstrap" in result.stdout
