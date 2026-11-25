from __future__ import annotations

from pathlib import Path

from context_router import load_route_policy, select_routes

POLICY = Path(__file__).resolve().parents[1] / "assets" / "routing" / "example-context-routes.json"
PROJECTS = {"ops_security_current", "src_100_course"}


def test_router_keeps_ops_and_course_separate_or_combines_explicitly() -> None:
    policy = load_route_policy(POLICY)

    ops = select_routes("判断这个域名是否可用于漏洞扫描", policy, PROJECTS)
    assert [item["route_id"] for item in ops["selected"]] == ["ops_conversation"]

    course = select_routes("100集课程里 Nuclei 怎么用", policy, PROJECTS)
    assert [item["route_id"] for item in course["selected"]] == ["src_course"]

    both = select_routes("结合100集课程和当前运维对话一起回答", policy, PROJECTS)
    assert {item["route_id"] for item in both["selected"]} == {"ops_conversation", "src_course"}
    assert both["ambiguous"] is True


def test_router_defaults_to_operational_route_for_weak_or_unknown_questions() -> None:
    policy = load_route_policy(POLICY)
    weak = select_routes("帮我看看这个", policy, PROJECTS)
    assert [item["route_id"] for item in weak["selected"]] == ["ops_conversation"]
    assert weak["fallback_used"] is True

    tool = select_routes("Nuclei 怎么用", policy, PROJECTS)
    assert [item["route_id"] for item in tool["selected"]] == ["src_course"]


def test_router_skips_projects_missing_from_profile() -> None:
    policy = load_route_policy(POLICY)
    result = select_routes("100集课程里 Nuclei 怎么用", policy, {"ops_security_current"})
    assert [item["route_id"] for item in result["selected"]] == ["ops_conversation"]
    assert any("not in the connection profile" in warning for warning in result["warnings"])
