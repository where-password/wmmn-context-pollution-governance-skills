from __future__ import annotations

import hashlib
from datetime import datetime, timezone

import pytest

from memory_core.skill_api import (
    DatabaseProfile,
    SkillApiBlocked,
    SkillBackend,
    apply_migration,
    plan_migration,
    verify_migration,
)

from .conftest import add_source, make_grant, make_token, make_write_input


def _profile(store) -> DatabaseProfile:
    return DatabaseProfile(
        environment="test",
        backend=SkillBackend.SQLITE,
        capabilities=("query", "health", "backup", "restore", "migrate"),
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
    outbox.run_once(owner="migration-test", limit=20)


def test_copy_only_migration_plan_apply_verify(tmp_path, store, writer, secret, outbox) -> None:
    _seed(store, writer, secret, outbox)
    source_hash = hashlib.sha256(store.path.read_bytes()).hexdigest()
    target = tmp_path / "migrated.sqlite3"
    plan = plan_migration(_profile(store), target_db=target)
    assert plan.status == "planned"
    assert plan.checks["target_absent"] is True

    result = apply_migration(_profile(store), plan, confirmation_token=plan.confirmation_token)
    assert result.verification.verified is True
    assert target.is_file()
    assert hashlib.sha256(store.path.read_bytes()).hexdigest() == source_hash
    assert result.verification.target_sha256 == source_hash

    verification = verify_migration(plan)
    assert verification.verified is True
    assert verification.counts.active == 1


def test_copy_only_migration_never_overwrites(tmp_path, store, writer, secret, outbox) -> None:
    _seed(store, writer, secret, outbox)
    target = tmp_path / "existing.sqlite3"
    target.write_bytes(b"existing")
    plan = plan_migration(_profile(store), target_db=target)
    assert plan.status == "blocked"
    assert "target_absent" in plan.blocked_reasons
    with pytest.raises(SkillApiBlocked, match="blocked"):
        apply_migration(_profile(store), plan, confirmation_token=plan.confirmation_token)


def test_copy_only_migration_rejects_wrong_token(tmp_path, store, writer, secret, outbox) -> None:
    _seed(store, writer, secret, outbox)
    plan = plan_migration(_profile(store), target_db=tmp_path / "migrated.sqlite3")
    with pytest.raises(SkillApiBlocked, match="confirmation"):
        apply_migration(_profile(store), plan, confirmation_token="wrong")