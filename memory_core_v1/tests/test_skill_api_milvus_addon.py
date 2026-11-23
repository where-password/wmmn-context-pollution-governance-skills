from pathlib import Path
import json

import pytest

from memory_core.skill_api import (
    connect_apply,
    connect_plan,
    local_milvus_install_apply,
    local_milvus_install_plan,
    milvus_bootstrap_apply,
    milvus_bootstrap_plan,
    milvus_connect_apply,
    milvus_connect_plan,
    milvus_reconcile_status,
    milvus_status,
    status,
    SkillApiBlocked,
)

from .conftest import add_source, make_grant, make_token, make_write_input

SKILL_ROOT = Path(__file__).resolve().parents[2] / "skills" / "database-installer-core"
COMPOSE = SKILL_ROOT / "assets" / "milvus" / "docker-compose.yml"
READ_ALIAS = "memory_read_alias_addon_test_001"
WRITE_ALIAS = "memory_write_alias_addon_test_001"
COLLECTION = "memory_core_addon_test_v1"


def _seed(store, writer, secret, outbox) -> None:
    add_source(store)
    grant = make_grant(store)
    result = writer.write(make_write_input(), make_token(secret, grant))
    assert result.status.value == "active"
    outbox.run_once(owner="milvus-addon-test", limit=20)


def _profile(store, tmp_path: Path) -> Path:
    plan = connect_plan(
        workspace_root=tmp_path,
        canonical_db=store.path,
        workspace_id="ws1",
        project_id="p1",
        domain_profile="novel",
        owner_principal_id="user-a",
    )
    return connect_apply(plan, confirmation_token=plan.confirmation_token).profile_path


def _route(tmp_path: Path, store, *, read_alias: str = READ_ALIAS, write_alias: str = WRITE_ALIAS) -> Path:
    payload = {
        "version": 1,
        "routes": [{
            "route_id": "milvus-addon-test",
            "workspace_id": "ws1",
            "domain_profile": "novel",
            "canonical_db": str(store.path),
            "milvus_uri": "http://127.0.0.1:19530",
            "read_alias": read_alias,
            "write_alias": write_alias,
            "physical_collection": COLLECTION,
            "allowed_projects": ["p1"],
            "embedding_model": "BAAI/bge-m3",
            "embedding_dimension": 1024,
            "index_schema_version": "memory-index-v1",
            "capabilities": ["query", "health", "backup", "write", "restore", "migrate"],
            "production_read_alias_name": "memory_read_alias_addon_prod",
            "production_write_alias_name": "memory_write_alias_addon_prod",
            "visibility_policy": {
                "require_project_scope": True,
                "require_owner_isolation": True,
                "enforce_sensitivity": True,
                "allow_global_kb": False,
                "allowed_visibilities": ["private", "workspace", "shared"],
            },
        }],
    }
    path = tmp_path / "milvus-route.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_sqlite_only_remains_usable_without_milvus_or_embedding(tmp_path, store) -> None:
    profile = _profile(store, tmp_path)
    assert status(profile).installation_status == "installed"
    result = milvus_status(
        profile,
        docker={"cli": False, "engine": False, "compose": False},
        embedding_key_env="TEST_MISSING_EMBEDDING_KEY",
    )
    assert result.status == "not_configured"
    assert result.vector == "disabled"
    assert result.embedding == "not_configured"
    assert result.checks["canonical_exists"] is True


def test_existing_milvus_connect_plan_apply_with_fake_read_index(tmp_path, store) -> None:
    profile = _profile(store, tmp_path)
    route = _route(tmp_path, store)
    plan = milvus_connect_plan(profile, route_path=route, fake=True)
    assert plan.status == "planned"
    assert plan.backend.value == "milvus"
    assert plan.read_alias == READ_ALIAS
    result = milvus_connect_apply(plan, confirmation_token=plan.confirmation_token)
    assert result.profile_path.is_file()
    payload = json.loads(result.profile_path.read_text(encoding="utf-8"))
    assert payload["backend"] == "milvus"
    assert "api_key" not in json.dumps(payload).casefold()


def test_milvus_connect_rejects_production_alias_and_conflict(tmp_path, store) -> None:
    profile = _profile(store, tmp_path)
    production_route = _route(tmp_path, store, read_alias="memory_read_alias_addon_prod")
    with pytest.raises(SkillApiBlocked, match="test/staging"):
        milvus_connect_plan(profile, route_path=production_route, fake=True)
    status_result = milvus_status(
        profile,
        route_path=production_route,
        fake=True,
        docker={"cli": True, "engine": True, "compose": True, "wsl": True},
        embedding_key_env="TEST_MISSING_EMBEDDING_KEY",
    )
    assert status_result.status == "blocked"
    assert status_result.checks["route_resolves"] is False

    route = _route(tmp_path, store)
    plan = milvus_connect_plan(profile, route_path=route, fake=True)
    plan = plan.model_copy(update={"status": "blocked", "blocked_reasons": ("collection_or_alias_conflict",)})
    with pytest.raises(SkillApiBlocked, match="blocked"):
        milvus_connect_apply(plan, confirmation_token=plan.confirmation_token)


def test_local_compose_plan_is_side_effect_free_and_fake_apply_is_explicit(tmp_path) -> None:
    compose_text = COMPOSE.read_text(encoding="utf-8")
    assert "milvusdb/milvus:v2.6.27" in compose_text
    assert "container_name:" not in compose_text
    assert '"127.0.0.1:19530:19530"' in compose_text
    assert '"127.0.0.1:9091:9091"' in compose_text
    assert "0.0.0.0:19530" not in compose_text
    assert "0.0.0.0:9091" not in compose_text
    root = tmp_path / "milvus-workspace"
    plan = local_milvus_install_plan(
        workspace_root=root,
        compose_template=COMPOSE,
        docker={"cli": True, "engine": True, "compose": True},
        env_refs={"MILVUS_MINIO_USER": True, "MILVUS_MINIO_PASSWORD": True},
    )
    assert plan.status == "planned"
    assert plan.image == "milvusdb/milvus:v2.6.27"
    assert not root.exists()

    calls = []
    result = local_milvus_install_apply(
        plan,
        confirmation_token=plan.confirmation_token,
        runner=lambda command, cwd: calls.append((command, cwd)) or 0,
    )
    assert result.status == "started"
    assert result.compose_file.is_file()
    assert calls and calls[0][0][1:3] == ["compose", "-p"]


def test_fake_vector_bootstrap_reconciles_to_zero_difference(tmp_path, store, writer, secret, outbox) -> None:
    _seed(store, writer, secret, outbox)
    profile = _profile(store, tmp_path)
    route = _route(tmp_path, store)
    plan = milvus_bootstrap_plan(profile, route_path=route, backend="fake")
    assert plan.status == "planned", plan.blocked_reasons
    result = milvus_bootstrap_apply(
        profile,
        plan,
        confirmation_token=plan.confirmation_token,
        backend="fake",
    )
    reconcile = milvus_reconcile_status(result)
    assert result.verify_passed is True
    assert reconcile["status"] == "zero_difference"
    assert reconcile["differences"] == 0


@pytest.mark.skip(reason="real Milvus smoke is opt-in; no production or external Milvus is used in CI")
def test_real_isolated_milvus_smoke_placeholder() -> None:
    pass


def test_bootstrap_fails_closed_on_existing_collection_or_alias(tmp_path, store) -> None:
    profile = _profile(store, tmp_path)
    route = _route(tmp_path, store)

    class ConflictProbe:
        def has_collection(self, name):
            return True

        def list_aliases(self):
            return {"aliases": [READ_ALIAS]}

    plan = milvus_bootstrap_plan(profile, route_path=route, backend="fake", resource_probe=ConflictProbe())
    assert plan.status == "blocked"
    assert "collection_absent" in plan.blocked_reasons
    assert "aliases_absent" in plan.blocked_reasons
