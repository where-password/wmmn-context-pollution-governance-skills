from pathlib import Path

from runtime_preflight import build_runtime_plan


def test_missing_python_and_dependencies_produce_repair_plan_without_side_effects(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    lock = tmp_path / "requirements-runtime.lock"
    lock.write_text("pydantic==2.14.3\n", encoding="utf-8")
    plan = build_runtime_plan(
        workspace_root=root,
        requirements_lock=lock,
        memory_core_source=tmp_path / "memory_core_v1",
        python_version=(3, 10),
        pydantic_present=False,
        pydantic_version=None,
        memory_core_present=False,
    )
    assert plan["status"] == "repair_required"
    assert plan["python_supported"] is False
    action_ids = {action["action_id"] for action in plan["actions"]}
    assert "select_supported_python" in action_ids
    assert "create_isolated_runtime" in action_ids
    assert "install_locked_dependencies" in action_ids
    assert not root.exists()
    assert all(action["requires_user_confirmation"] for action in plan["actions"])


def test_ready_runtime_plan_is_empty_and_read_only(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    lock = tmp_path / "requirements-runtime.lock"
    lock.write_text("pydantic==2.14.3\n", encoding="utf-8")
    plan = build_runtime_plan(
        workspace_root=root,
        requirements_lock=lock,
        python_version=(3, 13),
        pydantic_present=True,
        pydantic_version="2.14.3",
        memory_core_present=True,
    )
    assert plan["status"] == "ready"
    assert plan["pydantic_v2"] is True
    assert plan["actions"] == []
    assert not root.exists()


def test_pydantic_v1_is_blocked_with_dependency_repair(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    lock = tmp_path / "requirements-runtime.lock"
    lock.write_text("pydantic==2.14.3\n", encoding="utf-8")
    plan = build_runtime_plan(
        workspace_root=root,
        requirements_lock=lock,
        python_version=(3, 13),
        pydantic_present=True,
        pydantic_version="1.10.24",
        memory_core_present=True,
    )
    assert plan["status"] == "repair_required"
    assert plan["pydantic_present"] is True
    assert plan["pydantic_version"] == "1.10.24"
    assert plan["pydantic_major"] == 1
    assert plan["pydantic_v2"] is False
    action_ids = {action["action_id"] for action in plan["actions"]}
    assert "create_isolated_runtime" in action_ids
    assert "install_locked_dependencies" in action_ids
    assert not root.exists()


def test_unsupported_python_still_plans_isolated_runtime(tmp_path: Path) -> None:
    lock = tmp_path / "requirements-runtime.lock"
    lock.write_text("pydantic==2.14.3\n", encoding="utf-8")
    plan = build_runtime_plan(
        workspace_root=tmp_path / "workspace",
        requirements_lock=lock,
        python_version=(3, 14),
        pydantic_present=True,
        pydantic_version="2.14.3",
        memory_core_present=True,
    )
    action_ids = {action["action_id"] for action in plan["actions"]}
    assert "select_supported_python" in action_ids
    assert "create_isolated_runtime" in action_ids
    assert "install_locked_dependencies" in action_ids
