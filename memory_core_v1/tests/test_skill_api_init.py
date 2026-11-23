from __future__ import annotations

from pathlib import Path

import pytest

from memory_core.skill_api import (
    SkillApiBlocked,
    apply_init,
    health_check,
    load_database_profile,
    plan_init,
    query_ask,
    query_context,
)


def test_demo_init_query_and_health(tmp_path: Path) -> None:
    canonical = tmp_path / "demo.sqlite3"
    profile_path = tmp_path / "db-profile.json"
    plan = plan_init(mode="demo", canonical_db=canonical, profile_path=profile_path)
    assert plan.status == "planned"
    result = apply_init(plan, confirmation_token=plan.confirmation_token)
    assert result.seeded_records == 1
    assert canonical.is_file()
    profile = load_database_profile(profile_path)
    assert profile.mode == "demo"

    context = query_context(profile, query="示例灯塔")
    assert context.hits
    assert context.hits[0].content.startswith("示例灯塔")
    answer = query_ask(profile, query="示例灯塔")
    assert "示例灯塔" in answer.answer
    health = health_check(profile)
    assert health.counts["active"] == 1
    assert health.quality_status == "ok"


def test_real_new_init_creates_empty_canonical(tmp_path: Path) -> None:
    canonical = tmp_path / "real.sqlite3"
    profile_path = tmp_path / "db-profile.json"
    plan = plan_init(
        mode="real",
        canonical_db=canonical,
        profile_path=profile_path,
        workspace_id="ws1",
        project_id="p1",
        domain_profile="generic",
        owner_principal_id="user-a",
    )
    assert plan.status == "planned"
    apply_init(plan, confirmation_token=plan.confirmation_token)
    profile = load_database_profile(profile_path)
    assert profile.mode == "real"
    health = health_check(profile)
    assert health.counts["active"] == 0


def test_init_never_overwrites_existing_target(tmp_path: Path) -> None:
    canonical = tmp_path / "existing.sqlite3"
    canonical.write_bytes(b"existing")
    plan = plan_init(mode="demo", canonical_db=canonical, profile_path=tmp_path / "profile.json")
    assert plan.status == "blocked"
    assert "canonical_absent" in plan.blocked_reasons
    with pytest.raises(SkillApiBlocked, match="blocked"):
        apply_init(plan, confirmation_token=plan.confirmation_token)