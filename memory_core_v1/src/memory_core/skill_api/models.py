from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ..models import SensitivityLevel


class SkillApiError(RuntimeError):
    """Base error for the public skill API boundary."""


class SkillApiBlocked(SkillApiError):
    """The requested operation is unsafe or cannot be completed."""


class SkillApiConfigurationError(SkillApiError):
    """The requested profile or route configuration is invalid."""


class SkillBackend(StrEnum):
    SQLITE = "sqlite"
    MILVUS = "milvus"


class ProfileAttachPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_version: Literal["skill-profile-v1"] = "skill-profile-v1"
    status: Literal["planned", "blocked"] = "planned"
    plan_id: str
    confirmation_token: str
    mode: Literal["real"] = "real"
    environment: str
    backend: SkillBackend
    workspace_id: str
    project_id: str
    domain_profile: str
    owner_principal_id: str | None = None
    allowed_visibilities: tuple[str, ...] = ("private", "workspace", "shared")
    max_sensitivity: SensitivityLevel = SensitivityLevel.PRIVATE
    canonical_db: Path
    canonical_sha256: str
    route_id: str | None = None
    read_alias: str | None = None
    write_alias: str | None = None
    physical_collection: str | None = None
    embedding_model: str | None = None
    embedding_dimension: int | None = None
    index_schema_version: str | None = None
    metric_type: str | None = None
    profile_path: Path
    capabilities: tuple[str, ...] = ()
    checks: dict[str, bool] = Field(default_factory=dict)
    blocked_reasons: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class DatabaseProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_version: Literal["skill-profile-v1"] = "skill-profile-v1"
    profile_version: Literal["1.0.0"] = "1.0.0"
    mode: Literal["demo", "real"] = "real"
    environment: str
    backend: SkillBackend
    capabilities: tuple[str, ...] = ()
    workspace_id: str
    project_ids: tuple[str, ...]
    domain_profile: str
    owner_principal_id: str | None = None
    allowed_visibilities: tuple[str, ...] = ("private", "workspace", "shared")
    max_sensitivity: SensitivityLevel = SensitivityLevel.PRIVATE
    canonical_db: Path
    canonical_sha256: str
    route_id: str | None = None
    read_alias: str | None = None
    write_alias: str | None = None
    physical_collection: str | None = None
    embedding_model: str | None = None
    embedding_dimension: int | None = None
    index_schema_version: str | None = None
    metric_type: str | None = None
    plan_id: str
    created_at: datetime


class ProfileAttachResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_version: Literal["skill-profile-v1"] = "skill-profile-v1"
    status: Literal["applied"] = "applied"
    plan_id: str
    profile_path: Path
    changed: bool = True
    checks: dict[str, bool] = Field(default_factory=dict)
    warnings: tuple[str, ...] = ()