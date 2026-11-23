from __future__ import annotations

import hashlib
from datetime import datetime, timezone

import pytest

from memory_core.skill_api import (
    DatabaseProfile,
    SkillApiBlocked,
    SkillBackend,
    apply_restore,
    create_backup,
    list_backups,
    plan_restore,
    verify_backup,
)

from .conftest import add_source, make_grant, make_token, make_write_input


def _profile(store) -> DatabaseProfile:
    return DatabaseProfile(
        environment="test",
        backend=SkillBackend.SQLITE,
        capabilities=("query", "health", "backup", "restore"),
        workspace_id="ws1",
        project_ids=("p1",),
        domain_profile="novel",
        owner_principal_id="user-a",
        max_sensitivity="private",
        canonical_db=store.path,
        canonical_sha256="test-only",
        plan_id="test-plan",
        created_at=datetime.now(timezone.utc),
    )


def _seed(store, writer, secret, outbox) -> None:
    add_source(store)
    grant = make_grant(store)
    result = writer.write(make_write_input(), make_token(secret, grant))
    assert result.status.value == "active"
    outbox.run_once(owner="backup-test", limit=20)


def test_backup_create_verify_list_and_restore_plan(tmp_path, store, writer, secret, outbox) -> None:
    _seed(store, writer, secret, outbox)
    backup_dir = tmp_path / "backups"
    canonical_before = hashlib.sha256(store.path.read_bytes()).hexdigest()

    created = create_backup(_profile(store), backup_dir=backup_dir, retention=7)
    assert created.verification.verified is True
    assert created.manifest.backup_path.is_file()
    assert created.manifest.counts.active == 1
    assert hashlib.sha256(store.path.read_bytes()).hexdigest() == canonical_before

    listed = list_backups(backup_dir)
    assert len(listed.manifests) == 1
    assert listed.manifests[0].backup_id == created.manifest.backup_id

    verified = verify_backup(created.manifest.backup_path)
    assert verified.verified is True

    restore = plan_restore(_profile(store), backup_path=created.manifest.backup_path)
    assert restore.status == "planned"
    assert restore.target_db == store.path.resolve()
    assert restore.apply_available is True
    assert restore.checks["backup_verified"] is True


def test_backup_rejects_canonical_directory_and_short_retention(tmp_path, store) -> None:
    profile = _profile(store)
    with pytest.raises(SkillApiBlocked, match="canonical database directory"):
        create_backup(profile, backup_dir=store.path.parent, retention=7)
    with pytest.raises(SkillApiBlocked, match="retention"):
        create_backup(profile, backup_dir=tmp_path / "backups", retention=3)

def test_restore_apply_creates_prebackup_and_rollback(tmp_path, store, writer, secret, outbox) -> None:
    _seed(store, writer, secret, outbox)
    backup_dir = tmp_path / "backups"
    created = create_backup(_profile(store), backup_dir=backup_dir, retention=7)
    with store.transaction() as connection:
        connection.execute("DELETE FROM memory_fts")
    restore = plan_restore(_profile(store), backup_path=created.manifest.backup_path)
    result = apply_restore(
        _profile(store),
        restore,
        confirmation_token=restore.confirmation_token,
        pre_backup_dir=tmp_path / "pre-restore",
    )
    assert result.pre_restore_backup.is_file()
    assert result.rollback_db.is_file()
    assert result.checks == {
        "plan_valid": True,
        "pre_restore_backup": True,
        "staging_verified": True,
        "target_verified": True,
        "rollback_preserved": True,
    }
    verified = verify_backup(store.path)
    assert verified.verified is True
    assert verified.counts.fts == created.manifest.counts.fts

def test_restore_apply_rejects_active_worker_lock(tmp_path, store, writer, secret, outbox) -> None:
    _seed(store, writer, secret, outbox)
    created = create_backup(_profile(store), backup_dir=tmp_path / "backups", retention=7)
    restore = plan_restore(_profile(store), backup_path=created.manifest.backup_path)
    lock_file = tmp_path / "worker.lock"
    lock_file.write_text('{"status":"running"}', encoding="utf-8")
    with pytest.raises(SkillApiBlocked, match="worker lock"):
        apply_restore(
            _profile(store),
            restore,
            confirmation_token=restore.confirmation_token,
            pre_backup_dir=tmp_path / "pre-restore",
            lock_file=lock_file,
        )