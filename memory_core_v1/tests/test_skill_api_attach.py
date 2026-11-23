from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from memory_core.db import SQLiteStore
from memory_core.skill_api import (
    SkillApiBlocked,
    SkillBackend,
    apply_profile_attach,
    build_profile_attach_plan,
)


READ_ALIAS = "memory_read_alias_attach_test_001"
COLLECTION = "memory_core_attach_test_v1"


class FakeReadIndex:
    def aliases(self):
        return {READ_ALIAS: COLLECTION}

    def describe_collection(self):
        return {"collection_name": COLLECTION, "fields": ["memory_id", "embedding"]}

    def count(self):
        return 1

    def iter_rows(self, *, filter="", batch_size=1000, output_fields=None):
        yield {
            "memory_id": "m1",
            "version": 1,
            "embedding_model": "BAAI/bge-m3",
            "embedding_dim": 1024,
            "index_schema_version": "memory-index-v1",
        }


def _make_db(tmp_path: Path) -> Path:
    db = tmp_path / "canonical.sqlite3"
    store = SQLiteStore(db)
    store.migrate()
    store.ensure_workspace_project(
        workspace_id="ws1", workspace_name="Workspace 1",
        project_id="p1", project_name="Project 1",
    )
    return db


def _write_route(tmp_path: Path, db: Path, *, readonly: bool) -> Path:
    route = {
        "route_id": "attach-test",
        "workspace_id": "ws1",
        "domain_profile": "generic",
        "canonical_db": str(db),
        "milvus_uri": "test://milvus",
        "read_alias": READ_ALIAS,
        "physical_collection": COLLECTION,
        "allowed_projects": ["p1"],
        "embedding_model": "BAAI/bge-m3",
        "embedding_dimension": 1024,
        "index_schema_version": "memory-index-v1",
        "capabilities": ["query", "health", "backup"],
        "production_read_alias_name": "memory_read_alias_prod",
        "visibility_policy": {
            "require_project_scope": True,
            "require_owner_isolation": True,
            "enforce_sensitivity": True,
            "allow_global_kb": False,
            "allowed_visibilities": ["private", "workspace", "shared"],
        },
    }
    if not readonly:
        route["capabilities"] = ["query", "health", "backup", "write", "restore", "migrate"]
        route["write_alias"] = "memory_write_alias_attach_test_001"
        route["production_write_alias_name"] = "memory_write_alias_prod"
    path = tmp_path / "routes.json"
    path.write_text(json.dumps({"version": 1, "routes": [route]}), encoding="utf-8")
    return path


def test_profile_attach_plan_and_apply_only_write_local_profile(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    profile = tmp_path / "profile" / "db-profile.json"
    before = hashlib.sha256(db.read_bytes()).hexdigest()

    plan = build_profile_attach_plan(
        canonical_db=db,
        profile_path=profile,
        workspace_id="ws1",
        project_id="p1",
        domain_profile="generic",
        backend=SkillBackend.SQLITE,
    )

    assert plan.status == "planned"
    assert all(plan.checks.values())
    assert plan.capabilities == ("backup", "health", "migrate", "query", "restore", "write")
    assert not profile.exists()

    result = apply_profile_attach(plan, confirmation_token=plan.confirmation_token)
    assert result.status == "applied"
    assert profile.is_file()
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before
    payload = json.loads(profile.read_text(encoding="utf-8"))
    assert payload["backend"] == "sqlite"
    assert payload["canonical_db"] == str(db.resolve())
    assert payload["plan_id"] == plan.plan_id
    assert "api_key" not in json.dumps(payload).casefold()


def test_milvus_attach_requires_read_index_metadata(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    route = _write_route(tmp_path, db, readonly=True)
    plan = build_profile_attach_plan(
        canonical_db=db,
        profile_path=tmp_path / "db-profile.json",
        workspace_id="ws1",
        project_id="p1",
        domain_profile="generic",
        backend=SkillBackend.MILVUS,
        route_path=route,
        read_index=FakeReadIndex(),
    )

    assert plan.status == "planned"
    assert plan.capabilities == ("backup", "health", "query")
    assert plan.read_alias == READ_ALIAS
    assert plan.write_alias is None
    assert plan.checks["read_alias_target_matches"] is True
    assert plan.checks["embedding_model_matches"] is True
    assert plan.checks["embedding_dimension_matches"] is True
    assert plan.checks["index_schema_version_matches"] is True


def test_milvus_attach_without_read_index_is_blocked(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    route = _write_route(tmp_path, db, readonly=True)
    plan = build_profile_attach_plan(
        canonical_db=db,
        profile_path=tmp_path / "db-profile.json",
        workspace_id="ws1",
        project_id="p1",
        domain_profile="generic",
        backend=SkillBackend.MILVUS,
        route_path=route,
        read_index=None,
    )
    assert plan.status == "blocked"
    assert "read_index_available" in plan.blocked_reasons
    with pytest.raises(SkillApiBlocked, match="blocked"):
        apply_profile_attach(plan, confirmation_token=plan.confirmation_token)


def test_profile_attach_rejects_wrong_confirmation_and_drift(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    plan = build_profile_attach_plan(
        canonical_db=db,
        profile_path=tmp_path / "db-profile.json",
        workspace_id="ws1",
        project_id="p1",
        domain_profile="generic",
        backend=SkillBackend.SQLITE,
    )

    with pytest.raises(SkillApiBlocked, match="confirmation"):
        apply_profile_attach(plan, confirmation_token="wrong")

    db.write_bytes(db.read_bytes() + b"\n")
    with pytest.raises(SkillApiBlocked, match="changed"):
        apply_profile_attach(plan, confirmation_token=plan.confirmation_token)