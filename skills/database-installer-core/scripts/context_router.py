from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from memory_core.skill_api import SkillApiBlocked, build_context_pack, load_database_profile

CONTRACT_VERSION = "context-auto-v1"
ROUTE_POLICY_VERSION = "context-auto-routes-v1"
ROUTER_VERSION = "weighted-patterns-v1"


def _as_pattern_list(value: Any, name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise SkillApiBlocked(f"{name} must be a list of non-empty regex strings")
    return value


def load_route_policy(path: str | Path) -> dict[str, Any]:
    source = Path(path).expanduser().resolve()
    if not source.is_file() or source.is_symlink():
        raise SkillApiBlocked("route policy must be an existing regular JSON file")
    try:
        raw = json.loads(source.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise SkillApiBlocked("route policy is not valid UTF-8 JSON") from exc
    if not isinstance(raw, dict) or raw.get("contract_version") != ROUTE_POLICY_VERSION:
        raise SkillApiBlocked("route policy contract_version is unsupported")
    routes = raw.get("routes")
    if not isinstance(routes, list) or not routes:
        raise SkillApiBlocked("route policy routes must be a non-empty list")
    default_route_id = raw.get("default_route_id")
    if not isinstance(default_route_id, str) or not default_route_id.strip():
        raise SkillApiBlocked("route policy default_route_id is required")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in routes:
        if not isinstance(item, dict):
            raise SkillApiBlocked("each route policy entry must be an object")
        route_id = item.get("route_id")
        source_label = item.get("source_label")
        project_id = item.get("project_id")
        if not isinstance(route_id, str) or not route_id.strip():
            raise SkillApiBlocked("route_id must be a non-empty string")
        if route_id in seen:
            raise SkillApiBlocked("route_id values must be unique")
        seen.add(route_id)
        if not isinstance(source_label, str) or not source_label.strip():
            raise SkillApiBlocked(f"route {route_id} source_label is required")
        if not isinstance(project_id, str) or not project_id.strip():
            raise SkillApiBlocked(f"route {route_id} project_id is required")
        strong = _as_pattern_list(item.get("strong_patterns"), f"route {route_id} strong_patterns")
        weak = _as_pattern_list(item.get("weak_patterns"), f"route {route_id} weak_patterns")
        domain_profile = item.get("domain_profile")
        if domain_profile is not None and (not isinstance(domain_profile, str) or not domain_profile.strip()):
            raise SkillApiBlocked(f"route {route_id} domain_profile must be a non-empty string when supplied")
        threshold = item.get("threshold", 2)
        if isinstance(threshold, bool) or not isinstance(threshold, int) or threshold < 1:
            raise SkillApiBlocked(f"route {route_id} threshold must be a positive integer")
        for pattern in [*strong, *weak]:
            try:
                re.compile(pattern, re.IGNORECASE)
            except re.error as exc:
                raise SkillApiBlocked(f"invalid route regex: {pattern}") from exc
        normalized.append({
            "route_id": route_id,
            "source_label": source_label,
            "project_id": project_id,
            "domain_profile": domain_profile,
            "strong_patterns": strong,
            "weak_patterns": weak,
            "threshold": threshold,
            "enabled": bool(item.get("enabled", True)),
        })
    if default_route_id not in {item["route_id"] for item in normalized}:
        raise SkillApiBlocked("default_route_id does not name a configured route")
    return {
        "contract_version": ROUTE_POLICY_VERSION,
        "default_route_id": default_route_id,
        "combination_patterns": _as_pattern_list(raw.get("combination_patterns"), "combination_patterns"),
        "routes": normalized,
    }


def _single_project_policy(profile: Any) -> dict[str, Any]:
    if len(profile.project_ids) != 1:
        raise SkillApiBlocked("route policy is required when the profile has multiple projects")
    project_id = profile.project_ids[0]
    return {
        "contract_version": ROUTE_POLICY_VERSION,
        "default_route_id": "default",
        "combination_patterns": [],
        "routes": [{
            "route_id": "default",
            "source_label": "本地记忆",
            "project_id": project_id,
            "domain_profile": None,
            "strong_patterns": [],
            "weak_patterns": [],
            "threshold": 1,
            "enabled": True,
        }],
    }


def _score_route(query: str, route: dict[str, Any]) -> tuple[int, list[dict[str, Any]]]:
    score = 0
    matched: list[dict[str, Any]] = []
    for weight, field in ((3, "strong_patterns"), (1, "weak_patterns")):
        for pattern in route[field]:
            if re.search(pattern, query, re.IGNORECASE):
                score += weight
                matched.append({"pattern": pattern, "weight": weight})
    return score, matched


def select_routes(query: str, policy: dict[str, Any], available_projects: set[str]) -> dict[str, Any]:
    if not isinstance(query, str) or not query.strip():
        raise SkillApiBlocked("query must be a non-empty string")
    considered: list[dict[str, Any]] = []
    available: list[dict[str, Any]] = []
    warnings: list[str] = []
    for route in policy["routes"]:
        score, matched = _score_route(query, route)
        entry = {
            "route_id": route["route_id"],
            "source_label": route["source_label"],
            "project_id": route["project_id"],
            "domain_profile": route.get("domain_profile") or None,
            "score": score,
            "matched": matched,
            "available": route["enabled"] and route["project_id"] in available_projects,
        }
        considered.append(entry)
        if not route["enabled"]:
            warnings.append(f"route {route['route_id']} is disabled")
        elif route["project_id"] not in available_projects:
            warnings.append(f"route {route['route_id']} project is not in the connection profile")
        else:
            available.append({**entry, "threshold": route["threshold"]})

    if not available:
        raise SkillApiBlocked("route policy has no available project in the connection profile")
    default = next((item for item in available if item["route_id"] == policy["default_route_id"]), None)
    if default is None:
        raise SkillApiBlocked("default route project is not available in the connection profile")
    selected = [item for item in available if item["score"] >= item["threshold"]]
    fallback = not selected
    if fallback:
        selected = [default]
    combination = any(re.search(pattern, query, re.IGNORECASE) for pattern in policy["combination_patterns"])
    if combination and default not in selected:
        selected.append(default)
    selected_ids = {item["route_id"] for item in selected}
    ordered = [item for item in available if item["route_id"] in selected_ids]
    return {
        "strategy": ROUTER_VERSION,
        "query": query,
        "selected": ordered,
        "considered": considered,
        "ambiguous": len(ordered) > 1,
        "fallback_used": fallback,
        "warnings": warnings,
    }


def build_auto_context(
    profile: str | Path,
    *,
    query: str,
    route_file: str | Path | None = None,
    token_budget: int = 3500,
    top_k: int = 10,
    max_sensitivity: str | None = None,
    read_index: Any | None = None,
    embedding: Any | None = None,
) -> dict[str, Any]:
    profile_value = load_database_profile(profile)
    policy = load_route_policy(route_file) if route_file else _single_project_policy(profile_value)
    routing = select_routes(query, policy, set(profile_value.project_ids))
    selected = routing["selected"]
    if token_budget < 1000 * len(selected):
        raise SkillApiBlocked(f"token_budget must be at least {1000 * len(selected)} for selected routes")
    selected.sort(key=lambda item: next(
        index for index, route in enumerate(policy["routes"]) if route["route_id"] == item["route_id"]
    ))
    packs = []
    total = len(selected)
    base, remainder = divmod(token_budget, total)
    for index, route in enumerate(selected):
        route_budget = base + (1 if index < remainder else 0)
        route_profile = profile_value.model_copy(update={
            "domain_profile": route.get("domain_profile") or profile_value.domain_profile,
            "project_ids": (route["project_id"],),
        })
        pack = build_context_pack(
            route_profile,
            query=query,
            project_id=route["project_id"],
            token_budget=route_budget,
            max_sensitivity=max_sensitivity,
            top_k=top_k,
            read_index=read_index,
            embedding=embedding,
        )
        packs.append({
            "route_id": route["route_id"],
            "source_label": route["source_label"],
            "project_id": route["project_id"],
            "domain_profile": route_profile.domain_profile,
            "token_budget": route_budget,
            "context_pack": pack.model_dump(mode="json"),
        })
    return {
        "contract_version": CONTRACT_VERSION,
        "mode": "context_auto",
        "router_version": ROUTER_VERSION,
        "query": query,
        "routing": routing,
        "packs": packs,
        "cross_source_rules": [
            "Treat current operational decisions as authoritative over general course material.",
            "Treat course material as reference knowledge, not as proof about the current environment.",
            "If no source has useful evidence, do not fabricate memory and answer without claiming database support.",
            "Keep source labels visible when combining multiple packs.",
        ],
    }
