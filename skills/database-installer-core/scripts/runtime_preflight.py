from __future__ import annotations

"""Dependency-free runtime preflight for the database installer Skill.

This module deliberately uses only the Python standard library so it can still
produce a repair plan when Pydantic or memory_core cannot be imported.
"""

import argparse
from importlib import metadata
import importlib.util
import json
import platform
import sys
from pathlib import Path
from typing import Any

SUPPORTED_PYTHON_MIN = (3, 11)
SUPPORTED_PYTHON_MAX = (3, 13)
RUNTIME_LOCK_NAME = "requirements-runtime.lock"


def python_supported(version: tuple[int, int] | None = None) -> bool:
    current = version or sys.version_info[:2]
    return SUPPORTED_PYTHON_MIN <= current <= SUPPORTED_PYTHON_MAX


def _module_present(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        return False


def _distribution_version(name: str) -> str | None:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


def _major_version(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(value.split(".", 1)[0])
    except ValueError:
        return None


def build_runtime_plan(
    *,
    workspace_root: str | Path,
    requirements_lock: str | Path,
    memory_core_source: str | Path | None = None,
    python_version: tuple[int, int] | None = None,
    pydantic_present: bool | None = None,
    pydantic_version: str | None = None,
    memory_core_present: bool | None = None,
    python_exe: str | None = None,
) -> dict[str, Any]:
    """Return a deterministic, non-executing repair plan."""

    root = Path(workspace_root).resolve()
    lock = Path(requirements_lock).resolve()
    runtime_root = root / ".runtime"
    venv_root = runtime_root / "venv"
    runtime_python = venv_root / ("Scripts/python.exe" if platform.system() == "Windows" else "bin/python")
    version = python_version or sys.version_info[:2]
    pydantic_ok = _module_present("pydantic") if pydantic_present is None else pydantic_present
    detected_pydantic_version = pydantic_version if pydantic_version is not None else (_distribution_version("pydantic") if pydantic_ok else None)
    pydantic_major = _major_version(detected_pydantic_version)
    pydantic_v2 = pydantic_ok and pydantic_major == 2
    core_ok = _module_present("memory_core") if memory_core_present is None else memory_core_present
    supported = python_supported(version)
    actions: list[dict[str, Any]] = []

    if not supported:
        actions.append({
            "action_id": "select_supported_python",
            "required": True,
            "requires_user_confirmation": True,
            "summary": "安装或选择 Python 3.11–3.13；V1 不接受其他解释器。",
            "options": [
                "提供已下载的受控便携 Python 3.11–3.13 路径。",
                "在用户明确确认后运行官方 Python 安装命令；本命令不会自动执行 UAC。",
            ],
            "commands": [
                ["winget", "install", "--id", "Python.Python.3.13", "-e", "--scope", "user"],
            ],
            "manual_url": "https://www.python.org/downloads/windows/",
            "notes": "若需要管理员权限、BIOS 设置、重启或安全软件放行，应单独提示用户完成该一步。",
        })

    if not supported or not pydantic_v2 or not core_ok:
        source_args = [str(runtime_python), "-m", "pip", "install", "--no-deps", "-e", str(memory_core_source)] if memory_core_source else []
        create_command = [str(python_exe), "-m", "venv", str(venv_root)] if python_exe else ["py", "-3.13", "-m", "venv", str(venv_root)]
        actions.extend([
            {
                "action_id": "create_isolated_runtime",
                "required": True,
                "requires_user_confirmation": True,
                "summary": "在 workspace 内创建隔离 venv，不修改系统 Python。",
                "commands": [create_command],
                "notes": "如果用户提供便携 Python，请使用该解释器路径。",
            },
            {
                "action_id": "install_locked_dependencies",
                "required": True,
                "requires_user_confirmation": True,
                "summary": "只向隔离 venv 安装锁文件中的固定版本，并确保 Pydantic 为 >=2,<3。",
                "commands": [[str(runtime_python), "-m", "pip", "install", "--requirement", str(lock)]],
                "notes": "安装依赖需要联网；失败时保留计划，不污染系统 Python。",
            },
        ])
        if source_args:
            actions.append({
                "action_id": "install_memory_core_source",
                "required": True,
                "requires_user_confirmation": True,
                "summary": "将受支持的 memory_core 包安装到同一隔离 venv。",
                "commands": [source_args],
                "notes": "正式分发应使用已构建 wheel；本地开发可使用 editable source。",
            })

    return {
        "status": "ready" if supported and pydantic_v2 and core_ok else "repair_required",
        "python_version": f"{version[0]}.{version[1]}",
        "python_supported": supported,
        "pydantic_present": pydantic_ok,
        "pydantic_version": detected_pydantic_version,
        "pydantic_major": pydantic_major,
        "pydantic_v2": pydantic_v2,
        "memory_core_present": core_ok,
        "runtime_root": str(runtime_root),
        "venv_root": str(venv_root),
        "requirements_lock": str(lock),
        "actions": actions,
        "notes": [
            "本计划只描述动作，不自动安装 Python、依赖或 Docker。",
            "UAC、安全软件、BIOS、重启等系统级步骤必须由用户明确授权。",
            "V1 不安装 Docker、不连接 Milvus、不修改生产 alias。",
        ],
    }


def preflight_payload(
    error: str,
    *,
    workspace_root: str | Path,
    requirements_lock: str | Path,
    memory_core_source: str | Path | None = None,
) -> dict[str, Any]:
    plan = build_runtime_plan(
        workspace_root=workspace_root,
        requirements_lock=requirements_lock,
        memory_core_source=memory_core_source,
    )
    return {
        "status": "blocked",
        "error": {"code": "runtime_dependency_missing", "message": error},
        "repair_plan": plan,
    }


def emit_plan(plan: dict[str, Any]) -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(plan, ensure_ascii=False, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="memory-db-runtime-preflight")
    parser.add_argument("--workspace-root", required=True)
    parser.add_argument("--requirements-lock", required=True)
    parser.add_argument("--memory-core-source", default=None)
    parser.add_argument("--python-exe", default=None)
    args = parser.parse_args(argv)
    plan = build_runtime_plan(
        workspace_root=args.workspace_root,
        requirements_lock=args.requirements_lock,
        memory_core_source=args.memory_core_source,
        python_exe=args.python_exe,
    )
    emit_plan(plan)
    return 0 if plan["status"] == "ready" else 2


if __name__ == "__main__":
    raise SystemExit(main())
