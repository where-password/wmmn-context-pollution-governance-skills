from __future__ import annotations

from datetime import datetime, timedelta, timezone

from memory_core.db import SQLiteStore
from memory_core.models import AccessGrant, AccessScope, SensitivityLevel
from memory_core.skill_api import DatabaseProfile, SkillBackend, cleanup_local_grants


def _grant(grant_id: str, *, expires_at: datetime, revoked_at: datetime | None = None) -> AccessGrant:
    now = datetime.now(timezone.utc)
    return AccessGrant(
        grant_id=grant_id,
        principal_id="local-user",
        workspace_id="ws1",
        scope=AccessScope(
            project_ids=["p1"],
            domains=["generic"],
            max_sensitivity=SensitivityLevel.PRIVATE,
        ),
        actions=["write", "delete"],
        expires_at=expires_at,
        purpose="grant cleanup test",
        created_at=now,
        revoked=revoked_at is not None,
        revoked_at=revoked_at,
        revoked_reason="test" if revoked_at else None,
    )


def test_cleanup_local_grants_keeps_recent_audit_window(tmp_path) -> None:
    db_path = tmp_path / "memory.sqlite3"
    store = SQLiteStore(db_path)
    store.migrate()
    now = datetime.now(timezone.utc)
    store.save_grant(_grant("old-expired", expires_at=now - timedelta(days=10)))
    store.save_grant(_grant(
        "old-revoked",
        expires_at=now + timedelta(days=1),
        revoked_at=now - timedelta(days=10),
    ))
    store.save_grant(_grant("active", expires_at=now + timedelta(days=1)))
    store.save_grant(_grant(
        "recent-revoked",
        expires_at=now + timedelta(days=1),
        revoked_at=now - timedelta(days=1),
    ))

    profile = DatabaseProfile(
        environment="test",
        backend=SkillBackend.SQLITE,
        capabilities=("health", "write"),
        workspace_id="ws1",
        project_ids=("p1",),
        domain_profile="generic",
        owner_principal_id="local-user",
        canonical_db=db_path,
        canonical_sha256="test-only",
        plan_id="grant-cleanup-test",
        created_at=now,
    )
    result = cleanup_local_grants(profile, retention_days=7)
    assert result.removed_grants == 2
    assert store.get_grant("old-expired") is None
    assert store.get_grant("old-revoked") is None
    assert store.get_grant("active") is not None
    assert store.get_grant("recent-revoked") is not None
