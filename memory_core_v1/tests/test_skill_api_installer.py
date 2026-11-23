from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import memory_core.skill_api.installer as installer_module
from memory_core.skill_api import (
    CapabilitySet,
    connect_apply,
    connect_plan,
    demo_apply,
    demo_plan,
    doctor,
    export_profile,
    install_apply,
    install_plan,
    installer_backup,
    load_connection_profile,
    restore_apply,
    restore_plan,
    sample_apply,
    sample_plan,
    scan_for_secrets,
    self_test,
    smoke,
    status,
    upgrade_apply,
    upgrade_plan,
)

MANIFEST = Path(__file__).resolve().parents[2] / "skills" / "database-installer-core" / "assets" / "demo" / "demo_manifest.json"


def test_doctor_and_demo_plan_are_read_only(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    health = doctor(root)
    assert health.status == "ok"
    assert health.checks["pydantic_v2"] is True
    assert health.details["pydantic_version"].split(".", 1)[0] == "2"
    plan = demo_plan(workspace_root=root, manifest_path=MANIFEST)
    assert plan.status == "planned"
    assert not root.exists()
    assert plan.dataset_hash == "ae39eb93df1c009e38c85840b92c2e14da8b3ca984778e3f9303dfa66ff73ec9"


def test_demo_apply_is_deterministic_idempotent_and_smokeable(tmp_path: Path) -> None:
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    plan_a = demo_plan(workspace_root=root_a, manifest_path=MANIFEST)
    plan_b = demo_plan(workspace_root=root_b, manifest_path=MANIFEST)
    applied = demo_apply(plan_a, confirmation_token=plan_a.confirmation_token)
    again = demo_apply(plan_a, confirmation_token=plan_a.confirmation_token)
    applied_b = demo_apply(plan_b, confirmation_token=plan_b.confirmation_token)
    assert applied.status == "applied"
    assert again.status == "already_installed"
    assert applied_b.status == "applied"
    assert applied.dataset_hash == applied_b.dataset_hash == plan_a.dataset_hash
    assert applied.record_count == 3
    assert applied.capabilities == CapabilitySet(sqlite="installed", fts="available", vector="disabled", embedding="not_configured")

    profile = load_connection_profile(applied.profile_path)
    assert profile.portable is True
    assert profile.secret_refs == ()
    result = smoke(profile_path=applied.profile_path)
    assert result.status == "ok"
    assert all(result.checks.values())
    install_status = status(applied.profile_path)
    assert install_status.status == "ok"
    assert install_status.installation_status == "installed"


def test_profile_export_contains_no_secret_values(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    plan = demo_plan(workspace_root=root, manifest_path=MANIFEST)
    applied = demo_apply(plan, confirmation_token=plan.confirmation_token)
    output = tmp_path / "export.json"
    result = export_profile(applied.profile_path, output)
    payload = output.read_text(encoding="utf-8")
    assert result.portable is True
    assert result.secret_refs == ()
    lowered = payload.casefold()
    assert "api_key" not in lowered
    assert "password" not in lowered
    assert "authorization" not in lowered
    parsed = json.loads(payload)
    assert parsed["workspace_root"] == str(root.resolve())
    assert parsed["canonical_db"] == "data/memory.sqlite3"

def test_doctor_blocks_when_sqlite_probe_fails(tmp_path, monkeypatch) -> None:
    import memory_core.skill_api.installer as installer

    def fail_connect(*args, **kwargs):
        raise sqlite3.Error("simulated unavailable sqlite")

    monkeypatch.setattr(installer.sqlite3, "connect", fail_connect)
    result = doctor(tmp_path / "workspace")
    assert result.status == "blocked"
    assert result.checks["sqlite_available"] is False
    assert result.checks["fts5_trigram"] is False

def test_self_test_is_temporary_and_cleans_up(tmp_path: Path) -> None:
    result = self_test(MANIFEST)
    assert result.status == "passed"
    assert result.cleanup_verified is True
    assert all(result.checks.values())
    assert not list(tmp_path.glob("database-installer-self-test-*"))


def test_install_creates_empty_sqlite_and_sample_is_opt_in(tmp_path: Path) -> None:
    install_plan_result = install_plan(
        workspace_root=tmp_path / "real",
        workspace_id="ws-real",
        project_id="project-real",
        domain_profile="generic",
        owner_principal_id="owner-real",
    )
    assert install_plan_result.status == "planned"
    installed = install_apply(install_plan_result, confirmation_token=install_plan_result.confirmation_token)
    assert installed.canonical_db.is_file()
    profile = load_connection_profile(installed.profile_path)
    assert profile.mode == "real"
    assert profile.capabilities.vector == "disabled"
    assert profile.capabilities.embedding == "not_configured"

    sample_root = tmp_path / "teaching-sample"
    sample_plan_result = sample_plan(workspace_root=sample_root, manifest_path=MANIFEST)
    assert sample_plan_result.status == "planned"
    assert sample_plan_result.confirmation_token.startswith("sample:")
    sample_result = sample_apply(sample_plan_result, confirmation_token=sample_plan_result.confirmation_token)
    assert sample_result.canonical_db.is_file()
    assert sample_root.is_dir()
    assert not (tmp_path / "real" / ".runtime" / "self-test").exists()


def test_connect_marks_external_absolute_database_nonportable(tmp_path: Path) -> None:
    source_plan = install_plan(
        workspace_root=tmp_path / "source",
        workspace_id="ws-source",
        project_id="project-source",
        domain_profile="generic",
        owner_principal_id="owner-source",
    )
    source = install_apply(source_plan, confirmation_token=source_plan.confirmation_token)
    plan = connect_plan(
        workspace_root=tmp_path / "connected",
        canonical_db=source.canonical_db,
        workspace_id="ws-source",
        project_id="project-source",
        domain_profile="generic",
        owner_principal_id="owner-source",
    )
    assert plan.status == "planned"
    assert plan.portable is False
    connected = connect_apply(plan, confirmation_token=plan.confirmation_token)
    profile = load_connection_profile(connected.profile_path)
    assert profile.portable is False
    assert Path(profile.canonical_db).is_absolute()
    assert status(connected.profile_path).installation_status == "installed"


def test_secret_scan_blocks_known_secret_and_warns_for_hash(monkeypatch) -> None:
    canary = "canary-value-1234567890"
    monkeypatch.setenv("TEST_DB_SECRET", canary)
    blocked = scan_for_secrets(f'secret={canary}', secret_refs=("TEST_DB_SECRET",))
    assert blocked.status == "blocked"
    assert "known_secret_value" in blocked.blocked_reasons
    hash_result = scan_for_secrets("content_hash=" + "a" * 64)
    assert hash_result.status == "warning"
    assert "ordinary_hash_or_uuid" in hash_result.warnings
    assert scan_for_secrets('api_key=os.environ.get("EMBEDDING_API_KEY")').status != "blocked"
    assert scan_for_secrets('api_key="literal-secret-value"').status == "blocked"
    assert "openai_key" in scan_for_secrets("sk-abcdefghijklmnop1234567890").blocked_reasons
    assert "private_key" in scan_for_secrets("-----BEGIN RSA PRIVATE KEY-----").blocked_reasons
    assert "bearer_token" in scan_for_secrets("Bearer abcdefghijklmnop1234567890").blocked_reasons
    assert "secret_assignment" in scan_for_secrets('token="abcdefghijklmnop"').blocked_reasons
    assert "unquoted_secret_assignment" in scan_for_secrets("password=abcdefghijklmnop").blocked_reasons
    assert "credentialed_uri" in scan_for_secrets("https://user:password@example.test").blocked_reasons


def test_doctor_blocks_pydantic_v1(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(installer_module.pydantic, "VERSION", "1.10.24", raising=False)
    result = doctor(tmp_path / "workspace")
    assert result.status == "blocked"
    assert result.checks["pydantic_v2"] is False
    assert result.details["pydantic_version"] == "1.10.24"


def _install_for_restore(tmp_path: Path, name: str):
    root = tmp_path / name
    plan = install_plan(
        workspace_root=root,
        workspace_id=f"ws-{name}",
        project_id=f"project-{name}",
        domain_profile="generic",
        owner_principal_id=f"owner-{name}",
    )
    return root, install_apply(plan, confirmation_token=plan.confirmation_token)


def test_restore_plan_apply_restores_verified_backup(tmp_path: Path) -> None:
    root, installed = _install_for_restore(tmp_path, "restore")
    backup = installer_backup(installed.profile_path, root / "backups")
    connection = sqlite3.connect(installed.canonical_db)
    try:
        connection.execute("CREATE TABLE restore_marker (value TEXT)")
        connection.execute("INSERT INTO restore_marker(value) VALUES ('present')")
        connection.commit()
    finally:
        connection.close()

    plan = restore_plan(installed.profile_path, backup_path=backup.backup_path)
    assert plan.status == "planned"
    result = restore_apply(
        installed.profile_path,
        plan,
        confirmation_token=plan.confirmation_token,
        backup_dir=root / "restore-backups",
    )
    assert result.pre_restore_backup.is_file()
    assert result.rollback_db.is_file()
    assert all(result.checks.values()) is True
    connection = sqlite3.connect(installed.canonical_db)
    try:
        assert connection.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='restore_marker'"
        ).fetchone()[0] == 0
    finally:
        connection.close()


def test_restore_failure_restores_pre_restore_state(tmp_path: Path, monkeypatch) -> None:
    import memory_core.skill_api.backup as backup_api

    root, installed = _install_for_restore(tmp_path, "restore-fail")
    backup = installer_backup(installed.profile_path, root / "backups")
    connection = sqlite3.connect(installed.canonical_db)
    try:
        connection.execute("CREATE TABLE restore_marker (value TEXT)")
        connection.execute("INSERT INTO restore_marker(value) VALUES ('present')")
        connection.commit()
    finally:
        connection.close()
    plan = restore_plan(installed.profile_path, backup_path=backup.backup_path)
    canonical = installed.canonical_db.resolve()
    real_verify = backup_api.verify_backup

    def fail_final_target(path):
        result = real_verify(path)
        if Path(path).resolve() == canonical:
            return result.model_copy(update={"verified": False})
        return result

    monkeypatch.setattr(backup_api, "verify_backup", fail_final_target)
    import pytest
    with pytest.raises(Exception, match="previous database was restored"):
        restore_apply(
            installed.profile_path,
            plan,
            confirmation_token=plan.confirmation_token,
            backup_dir=root / "restore-backups",
        )
    connection = sqlite3.connect(installed.canonical_db)
    try:
        assert connection.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='restore_marker'"
        ).fetchone()[0] == 1
    finally:
        connection.close()


def _prepare_upgrade_migrations(tmp_path: Path, monkeypatch):
    import memory_core.db as core_db
    migration_dir = tmp_path / "migrations"
    migration_dir.mkdir()
    for source in core_db.MIGRATIONS_DIR.glob("*.sql"):
        (migration_dir / source.name).write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    (migration_dir / "006_upgrade_probe.sql").write_text(
        "CREATE TABLE IF NOT EXISTS upgrade_probe (id INTEGER PRIMARY KEY, value TEXT);\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(core_db, "MIGRATIONS_DIR", migration_dir)
    return migration_dir


def test_forward_upgrade_applies_pending_migration_with_backup(tmp_path: Path, monkeypatch) -> None:
    root, installed = _install_for_restore(tmp_path, "upgrade")
    _prepare_upgrade_migrations(tmp_path, monkeypatch)
    plan = upgrade_plan(installed.profile_path)
    assert plan.status == "planned"
    assert "006_upgrade_probe" in plan.pending_migrations
    result = upgrade_apply(
        installed.profile_path,
        plan,
        confirmation_token=plan.confirmation_token,
        backup_dir=root / "upgrades",
    )
    assert result.status == "applied"
    assert result.pre_upgrade_backup is not None and result.pre_upgrade_backup.is_file()
    assert result.rollback_db is not None and result.rollback_db.is_file()
    connection = sqlite3.connect(installed.canonical_db)
    try:
        assert connection.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='upgrade_probe'"
        ).fetchone()[0] == 1
    finally:
        connection.close()


def test_forward_upgrade_failure_restores_previous_database(tmp_path: Path, monkeypatch) -> None:
    root, installed = _install_for_restore(tmp_path, "upgrade-fail")
    _prepare_upgrade_migrations(tmp_path, monkeypatch)
    plan = upgrade_plan(installed.profile_path)
    import memory_core.skill_api.installer as installer_api
    real_verify = installer_api._verify_upgrade_database
    canonical = installed.canonical_db.resolve()

    def fail_after_replacement(path, *, expected_migrations):
        checks = real_verify(path, expected_migrations=expected_migrations)
        if Path(path).resolve() == canonical:
            connection = sqlite3.connect(canonical)
            try:
                has_probe = connection.execute(
                    "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='upgrade_probe'"
                ).fetchone()[0] == 1
            finally:
                connection.close()
            if has_probe:
                checks["injected_failure"] = False
        return checks

    monkeypatch.setattr(installer_api, "_verify_upgrade_database", fail_after_replacement)
    import pytest
    with pytest.raises(Exception, match="pre-upgrade state was restored"):
        upgrade_apply(
            installed.profile_path,
            plan,
            confirmation_token=plan.confirmation_token,
            backup_dir=root / "upgrades",
        )
    connection = sqlite3.connect(installed.canonical_db)
    try:
        assert connection.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='upgrade_probe'"
        ).fetchone()[0] == 0
    finally:
        connection.close()
