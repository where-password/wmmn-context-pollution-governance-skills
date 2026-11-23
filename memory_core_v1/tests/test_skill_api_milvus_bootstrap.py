from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from memory_core.fakes import FakeEmbedding, FakeVectorIndex
from memory_core.skill_api import (
    DatabaseProfile,
    SkillApiBlocked,
    SkillBackend,
    apply_milvus_bootstrap,
    plan_milvus_bootstrap,
)

from .conftest import add_source, make_grant, make_token, make_write_input


class FakeCleanupClient:
    def __init__(self) -> None:
        self.aliases: set[str] = set()
        self.collections: set[str] = set()

    def list_aliases(self):
        return {"aliases": sorted(self.aliases)}

    def drop_alias(self, *, alias):
        self.aliases.discard(alias)

    def has_collection(self, name: str) -> bool:
        return name in self.collections

    def drop_collection(self, name: str) -> None:
        self.collections.discard(name)


class FakeBootstrapIndex(FakeVectorIndex):
    def __init__(self, *, read_alias: str, write_alias: str, collection_name: str, fail_remaining: int = 0) -> None:
        super().__init__(read_alias=read_alias, write_alias=write_alias, fail_remaining=fail_remaining)
        self.collection_name = collection_name
        self.bound = False
        self.client = FakeCleanupClient()

    def ensure_collection(self, name: str | None = None):
        self.collections.setdefault(self.read_alias, {})
        self.collections.setdefault(self.write_alias, {})
        self.client.collections.add(self.collection_name)
        return True

    def bind_aliases(self, *, switch_if_exists: bool = False):
        if switch_if_exists:
            raise AssertionError("bootstrap must not switch aliases")
        self.bound = True
        self.client.aliases.update({self.read_alias, self.write_alias})

    def aliases(self):
        return {self.read_alias: self.collection_name, self.write_alias: self.collection_name}


class AvailabilityProbe:
    def has_collection(self, name: str) -> bool:
        return False

    def list_aliases(self):
        return {"aliases": []}


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


def _route(tmp_path: Path, store) -> Path:
    read_alias = "memory_read_alias_bootstrap_test_001"
    write_alias = "memory_write_alias_bootstrap_test_001"
    payload = {
        "version": 1,
        "routes": [{
            "route_id": "bootstrap-test",
            "workspace_id": "ws1",
            "domain_profile": "novel",
            "canonical_db": str(store.path),
            "milvus_uri": "test://milvus",
            "read_alias": read_alias,
            "write_alias": write_alias,
            "physical_collection": "memory_core_bootstrap_test_v1",
            "allowed_projects": ["p1"],
            "embedding_model": "BAAI/bge-m3",
            "embedding_dimension": 1024,
            "index_schema_version": "memory-index-v1",
            "capabilities": ["query", "health", "backup", "write", "restore", "migrate"],
            "production_read_alias_name": "memory_read_alias_prod",
            "production_write_alias_name": "memory_write_alias_prod",
            "visibility_policy": {
                "require_project_scope": True,
                "require_owner_isolation": True,
                "enforce_sensitivity": True,
                "allow_global_kb": False,
                "allowed_visibilities": ["private", "workspace", "shared"],
            },
        }],
    }
    path = tmp_path / "bootstrap-routes.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _seed(store, writer, secret, outbox) -> None:
    add_source(store)
    grant = make_grant(store)
    result = writer.write(make_write_input(), make_token(secret, grant))
    assert result.status.value == "active"
    outbox.run_once(owner="bootstrap-test", limit=20)


def test_isolated_milvus_bootstrap_reconciles_with_fake_index(tmp_path, store, writer, secret, outbox) -> None:
    _seed(store, writer, secret, outbox)
    route = _route(tmp_path, store)
    plan = plan_milvus_bootstrap(_profile(store), route_path=route, resource_probe=AvailabilityProbe())
    assert plan.status == "planned"
    assert plan.read_alias.endswith("_test_001")
    index = FakeBootstrapIndex(
        read_alias=plan.read_alias,
        write_alias=plan.write_alias,
        collection_name=plan.physical_collection,
    )
    result = apply_milvus_bootstrap(
        _profile(store),
        plan,
        confirmation_token=plan.confirmation_token,
        vector_index=index,
        embedding=FakeEmbedding(dimension=1024),
    )
    assert result.status == "applied"
    assert result.verify_passed is True
    assert result.differences == 0
    assert index.bound is True


def test_bootstrap_requires_test_staging_environment_and_absent_collection(tmp_path, store) -> None:
    route = _route(tmp_path, store)
    with pytest.raises(SkillApiBlocked, match="test/staging"):
        plan_milvus_bootstrap(_profile(store), route_path=route, environment="production")
    plan = plan_milvus_bootstrap(_profile(store), route_path=route, resource_probe=AvailabilityProbe())
    index = FakeBootstrapIndex(
        read_alias=plan.read_alias,
        write_alias=plan.write_alias,
        collection_name=plan.physical_collection,
    )
    index.ensure_collection = lambda name=None: False  # type: ignore[method-assign]
    with pytest.raises(SkillApiBlocked, match="already exists"):
        apply_milvus_bootstrap(
            _profile(store), plan,
            confirmation_token=plan.confirmation_token,
            vector_index=index,
            embedding=FakeEmbedding(dimension=1024),
        )

def test_failed_bootstrap_cleans_up_isolated_resources(tmp_path, store, writer, secret, outbox) -> None:
    _seed(store, writer, secret, outbox)
    route = _route(tmp_path, store)
    plan = plan_milvus_bootstrap(_profile(store), route_path=route, resource_probe=AvailabilityProbe())
    index = FakeBootstrapIndex(
        read_alias=plan.read_alias,
        write_alias=plan.write_alias,
        collection_name=plan.physical_collection,
        fail_remaining=1,
    )
    with pytest.raises(SkillApiBlocked, match="reconcile"):
        apply_milvus_bootstrap(
            _profile(store),
            plan,
            confirmation_token=plan.confirmation_token,
            vector_index=index,
            embedding=FakeEmbedding(dimension=1024),
        )
    assert index.client.collections == set()
    assert index.client.aliases == set()