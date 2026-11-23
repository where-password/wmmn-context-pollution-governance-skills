from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ..db import SQLiteStore
from .attach import load_database_profile
from .models import DatabaseProfile, SkillApiBlocked


class GrantCleanupResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_version: Literal["skill-grant-cleanup-v1"] = "skill-grant-cleanup-v1"
    status: Literal["completed"] = "completed"
    removed_grants: int = Field(ge=0)
    retention_days: int = Field(ge=0)


def cleanup_local_grants(
    profile: DatabaseProfile | str | Path,
    *,
    retention_days: int = 7,
) -> GrantCleanupResult:
    """Remove expired/revoked grants after a bounded local audit-retention window."""

    if isinstance(retention_days, bool) or retention_days < 0:
        raise SkillApiBlocked("retention_days must be a non-negative integer")
    profile_value = profile if isinstance(profile, DatabaseProfile) else load_database_profile(profile)
    canonical = profile_value.canonical_db.resolve()
    if not canonical.is_file():
        raise SkillApiBlocked("canonical database does not exist")
    store = SQLiteStore(canonical)
    removed = store.cleanup_access_grants(retention_seconds=retention_days * 86400)
    return GrantCleanupResult(removed_grants=removed, retention_days=retention_days)
