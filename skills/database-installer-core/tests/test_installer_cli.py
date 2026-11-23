from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path

import db_skill


def _json_output(capsys) -> dict:
    return json.loads(capsys.readouterr().out)


def test_help_prioritizes_real_paths_and_hides_compatibility_demo() -> None:
    help_text = db_skill._parser().format_help()
    assert "install" in help_text
    assert "connect" in help_text
    assert "self-test" in help_text
    assert "sample" in help_text
    assert "demo" not in help_text


def test_self_test_and_legacy_demo_leave_no_workspace_data(tmp_path: Path, capsys, monkeypatch) -> None:
    temp_root = tmp_path / "tmp"
    temp_root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temp_root))
    code = db_skill.main(["self-test", "--json"])
    self_test_payload = _json_output(capsys)
    assert code == 0
    assert self_test_payload["result"]["status"] == "passed"
    assert self_test_payload["result"]["cleanup_verified"] is True
    assert list(temp_root.glob("database-installer-self-test-*")) == []

    code = db_skill.main(["demo", "--json"])
    demo_payload = _json_output(capsys)
    assert code == 0
    assert demo_payload["status"] == "compatibility_warning"
    assert "不保留 Demo 数据" in demo_payload["warning"]
    assert list(temp_root.glob("database-installer-self-test-*")) == []


def test_install_connect_sample_status_backup_and_uninstall(tmp_path: Path, capsys) -> None:
    install_root = tmp_path / "real"
    install_plan_file = tmp_path / "install-plan.json"
    code = db_skill.main([
        "install", "plan", "--workspace-root", str(install_root),
        "--workspace-id", "ws-cli", "--project-id", "project-cli",
        "--domain-profile", "generic", "--owner-principal-id", "owner-cli",
        "--plan-out", str(install_plan_file), "--json",
    ])
    install_plan_payload = _json_output(capsys)
    assert code == 0
    assert install_plan_payload["result"]["status"] == "planned"

    install_plan = json.loads(install_plan_file.read_text(encoding="utf-8"))
    code = db_skill.main([
        "install", "apply", "--plan-file", str(install_plan_file),
        "--confirm-token", install_plan["confirmation_token"], "--json",
    ])
    installed = _json_output(capsys)
    assert code == 0
    assert installed["result"]["status"] == "installed"
    profile = installed["result"]["profile_path"]
    assert Path(installed["result"]["canonical_db"]).is_file()

    code = db_skill.main(["status", "--profile", profile, "--json"])
    status_payload = _json_output(capsys)
    assert code == 0
    assert status_payload["result"]["capabilities"] == {
        "sqlite": "installed", "fts": "available", "vector": "disabled", "embedding": "not_configured",
    }

    exported = tmp_path / "profile.json"
    code = db_skill.main(["profile", "export", "--profile", profile, "--output", str(exported), "--json"])
    assert code == 0
    exported_text = exported.read_text(encoding="utf-8").casefold()
    assert "api_key" not in exported_text
    assert "password" not in exported_text
    assert "authorization" not in exported_text
    _json_output(capsys)

    code = db_skill.main(["backup", "create", "--profile", profile, "--json"])
    backup_payload = _json_output(capsys)
    assert code == 0
    assert Path(backup_payload["result"]["backup_path"]).is_file()
    assert Path(backup_payload["result"]["profile_backup_path"]).is_file()

    canonical = Path(installed["result"]["canonical_db"])
    connection = sqlite3.connect(canonical)
    try:
        connection.execute("CREATE TABLE restore_marker (value TEXT)")
        connection.commit()
    finally:
        connection.close()
    restore_plan_file = tmp_path / "restore-plan.json"
    code = db_skill.main([
        "restore", "plan", "--profile", profile,
        "--backup", backup_payload["result"]["backup_path"],
        "--plan-out", str(restore_plan_file), "--json",
    ])
    assert code == 0
    _json_output(capsys)
    restore_plan = json.loads(restore_plan_file.read_text(encoding="utf-8"))
    code = db_skill.main([
        "restore", "apply", "--profile", profile,
        "--plan-file", str(restore_plan_file),
        "--confirm-token", restore_plan["confirmation_token"], "--json",
    ])
    assert code == 0
    _json_output(capsys)
    connection = sqlite3.connect(canonical)
    try:
        assert connection.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='restore_marker'"
        ).fetchone()[0] == 0
    finally:
        connection.close()

    upgrade_plan_file = tmp_path / "upgrade-plan.json"
    code = db_skill.main([
        "upgrade", "plan", "--profile", profile,
        "--plan-out", str(upgrade_plan_file), "--json",
    ])
    assert code == 0
    assert _json_output(capsys)["result"]["up_to_date"] is True
    upgrade_plan = json.loads(upgrade_plan_file.read_text(encoding="utf-8"))
    code = db_skill.main([
        "upgrade", "apply", "--profile", profile,
        "--plan-file", str(upgrade_plan_file),
        "--confirm-token", upgrade_plan["confirmation_token"], "--json",
    ])
    assert code == 0
    assert _json_output(capsys)["result"]["status"] == "already_current"

    connect_root = tmp_path / "connected"
    connect_plan_file = tmp_path / "connect-plan.json"
    source_db = installed["result"]["canonical_db"]
    code = db_skill.main([
        "connect", "plan", "--workspace-root", str(connect_root),
        "--canonical-db", source_db, "--workspace-id", "ws-cli",
        "--project-id", "project-cli", "--domain-profile", "generic",
        "--owner-principal-id", "owner-cli", "--plan-out", str(connect_plan_file), "--json",
    ])
    assert code == 0
    connect_plan = json.loads(connect_plan_file.read_text(encoding="utf-8"))
    assert connect_plan["portable"] is False
    _json_output(capsys)

    sample_root = tmp_path / "teaching-sample"
    sample_plan_file = tmp_path / "sample-plan.json"
    code = db_skill.main([
        "sample", "plan", "--workspace-root", str(sample_root),
        "--manifest", str(db_skill.DEFAULT_MANIFEST), "--plan-out", str(sample_plan_file), "--json",
    ])
    assert code == 0
    assert _json_output(capsys)["sample_only"] is True
    sample_plan = json.loads(sample_plan_file.read_text(encoding="utf-8"))
    code = db_skill.main([
        "sample", "apply", "--plan-file", str(sample_plan_file),
        "--confirm-token", sample_plan["confirmation_token"], "--json",
    ])
    assert code == 0
    assert _json_output(capsys)["sample_only"] is True
    assert (sample_root / "data" / "memory.sqlite3").is_file()

    code = db_skill.main(["uninstall", "--profile", profile, "--json"])
    assert code == 0
    assert Path(profile).is_file()
    assert Path(installed["result"]["canonical_db"]).is_file()
    _json_output(capsys)


def test_start_stop_are_explicitly_not_applicable(capsys) -> None:
    for command in ("start", "stop"):
        code = db_skill.main([command, "--json"])
        payload = _json_output(capsys)
        assert code == 2
        assert payload["status"] == "not_applicable"
        assert payload["error"]["code"] == "not_applicable"
        assert "不使用自有 daemon" in payload["error"]["message"]


def test_emit_blocks_known_secret_values(monkeypatch, capsys) -> None:
    canary = "db-cli-canary-123456789"
    monkeypatch.setenv("DB_CLI_SECRET", canary)
    db_skill._emit(
        {"command": "status", "result": {"message": f"unexpected {canary}"}},
        json_mode=True,
        secret_refs=("DB_CLI_SECRET",),
    )
    output = capsys.readouterr().out
    assert canary not in output
    payload = json.loads(output)
    assert payload["error"]["code"] == "secret_scan_blocked"


def test_milvus_status_and_existing_connect_with_fake_backend(tmp_path: Path, capsys, monkeypatch) -> None:
    monkeypatch.delenv("TEST_EMBEDDING_KEY", raising=False)
    workspace = tmp_path / "sqlite"
    plan_file = tmp_path / "install.json"
    code = db_skill.main([
        "install", "plan", "--workspace-root", str(workspace),
        "--workspace-id", "ws-milvus", "--project-id", "project-milvus",
        "--domain-profile", "generic", "--owner-principal-id", "owner-milvus",
        "--plan-out", str(plan_file), "--json",
    ])
    assert code == 0
    _json_output(capsys)
    install = json.loads(plan_file.read_text(encoding="utf-8"))
    code = db_skill.main([
        "install", "apply", "--plan-file", str(plan_file),
        "--confirm-token", install["confirmation_token"], "--json",
    ])
    assert code == 0
    profile = _json_output(capsys)["result"]["profile_path"]
    route = tmp_path / "route.json"
    route.write_text(json.dumps({
        "version": 1,
        "routes": [{
            "route_id": "milvus-cli-test",
            "workspace_id": "ws-milvus",
            "domain_profile": "generic",
            "canonical_db": str(workspace / "data" / "memory.sqlite3"),
            "milvus_uri": "http://127.0.0.1:19530",
            "read_alias": "memory_read_alias_cli_test_001",
            "write_alias": "memory_write_alias_cli_test_001",
            "physical_collection": "memory_core_cli_test_v1",
            "allowed_projects": ["project-milvus"],
            "embedding_model": "BAAI/bge-m3",
            "embedding_dimension": 1024,
            "index_schema_version": "memory-index-v1",
            "capabilities": ["query", "health", "backup", "write", "restore", "migrate"],
            "production_read_alias_name": "memory_read_alias_cli_prod",
            "production_write_alias_name": "memory_write_alias_cli_prod",
            "visibility_policy": {
                "require_project_scope": True,
                "require_owner_isolation": True,
                "enforce_sensitivity": True,
                "allow_global_kb": False,
                "allowed_visibilities": ["private", "workspace", "shared"],
            },
        }],
    }), encoding="utf-8")
    code = db_skill.main([
        "milvus", "status", "--profile", profile, "--route-file", str(route),
        "--fake", "--embedding-key-env", "TEST_EMBEDDING_KEY", "--json",
    ])
    status_payload = _json_output(capsys)
    assert code == 0
    assert status_payload["result"]["vector"] == "deferred"
    assert status_payload["result"]["embedding"] == "not_configured"

    milvus_plan_file = tmp_path / "milvus-connect.json"
    code = db_skill.main([
        "milvus", "connect", "plan", "--profile", profile,
        "--route-file", str(route), "--fake", "--plan-out", str(milvus_plan_file), "--json",
    ])
    assert code == 0
    _json_output(capsys)
    milvus_plan = json.loads(milvus_plan_file.read_text(encoding="utf-8"))
    code = db_skill.main([
        "milvus", "connect", "apply", "--plan-file", str(milvus_plan_file),
        "--confirm-token", milvus_plan["confirmation_token"], "--json",
    ])
    assert code == 0
    apply_payload = _json_output(capsys)
    assert Path(apply_payload["result"]["profile_path"]).is_file()

    bootstrap_plan_file = tmp_path / "milvus-bootstrap.json"
    code = db_skill.main([
        "milvus", "bootstrap", "plan", "--profile", profile,
        "--route-file", str(route), "--backend", "fake",
        "--plan-out", str(bootstrap_plan_file), "--json",
    ])
    assert code == 0
    _json_output(capsys)
    bootstrap_plan = json.loads(bootstrap_plan_file.read_text(encoding="utf-8"))
    code = db_skill.main([
        "milvus", "bootstrap", "apply", "--profile", profile,
        "--plan-file", str(bootstrap_plan_file),
        "--confirm-token", bootstrap_plan["confirmation_token"],
        "--backend", "fake", "--json",
    ])
    assert code == 0
    bootstrap_payload = _json_output(capsys)
    assert bootstrap_payload["reconcile"]["status"] == "zero_difference"
    assert bootstrap_payload["reconcile"]["differences"] == 0
