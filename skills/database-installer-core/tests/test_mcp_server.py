from __future__ import annotations

import json
from pathlib import Path

from memory_core.fakes import FakeEmbedding, FakeVectorIndex
from memory_core.outbox import OutboxWorker
from memory_core.profiles import ProfileRegistry
from memory_core.skill_api import apply_handoff_ingest_local

import mcp_server
from test_handoff_apply_cli import _apply_profile, _item, _write_fixture


def _json_text(tool_result: dict) -> dict:
    return json.loads(tool_result["content"][0]["text"])


def _prepare_memory(tmp_path: Path) -> tuple[Path, str]:
    item = _item(key="mwh_mcp_context", content="MCP context test memory.")
    profile, _, store = _apply_profile(tmp_path, [item])
    raw = json.loads(profile.read_text(encoding="utf-8"))
    raw["max_sensitivity"] = "private"
    profile.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    directory, _ = _write_fixture(
        tmp_path,
        [item],
        [(item, "approve", "2026-11-22T10:00:00+08:00")],
    )
    outcome = apply_handoff_ingest_local(
        canonical_db=store.path,
        handoff_dir=directory,
        handoff_id="handoff_database_skill_cli_test",
        workspace_id="ws_cli_handoff",
        project_id="project_cli_handoff",
        domain_profile="generic",
        profile_version="1.0.0",
        source_id="source_cli_handoff",
        written_memory_map={},
        principal_id="owner_cli_handoff",
    )
    memory_id = outcome.apply_result.write_results[0].memory_id
    worker = OutboxWorker(
        store=store,
        profiles=ProfileRegistry(),
        embedding=FakeEmbedding(),
        vector_index=FakeVectorIndex(),
        write_alias="memory_write_alias",
    )
    assert worker.run_once(owner="mcp-test")
    return profile, memory_id


def test_mcp_tools_expose_context_search_memory_and_health(tmp_path: Path) -> None:
    profile, memory_id = _prepare_memory(tmp_path)
    server = mcp_server.MemoryDatabaseMcpServer(profile)

    initialized = server.handle_message({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": "2025-06-18"},
    })
    assert initialized["result"]["protocolVersion"] == "2025-06-18"
    assert initialized["result"]["capabilities"]["tools"]["listChanged"] is False

    listed = server.handle_message({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    names = {tool["name"] for tool in listed["result"]["tools"]}
    assert names == {"context", "search", "get_memory", "health"}

    context_result = server.handle_message({
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {"name": "context", "arguments": {"query": "MCP", "token_budget": 3000}},
    })
    context_payload = _json_text(context_result["result"])
    assert context_payload["mode"] == "context_pack"
    assert context_payload["current"]

    search_result = server.handle_message({
        "jsonrpc": "2.0",
        "id": 4,
        "method": "tools/call",
        "params": {"name": "search", "arguments": {"query": "MCP", "top_k": 5}},
    })
    search_payload = _json_text(search_result["result"])
    assert search_payload["mode"] == "search_only"
    assert "memory_id" not in search_payload["hits"][0]

    memory_result = server.handle_message({
        "jsonrpc": "2.0",
        "id": 5,
        "method": "tools/call",
        "params": {"name": "get_memory", "arguments": {"memory_id": memory_id}},
    })
    memory_payload = _json_text(memory_result["result"])
    assert memory_payload["mode"] == "memory_detail"
    assert memory_payload["memory_id"] == memory_id

    health_result = server.handle_message({
        "jsonrpc": "2.0",
        "id": 6,
        "method": "tools/call",
        "params": {"name": "health", "arguments": {}},
    })
    health_payload = _json_text(health_result["result"])
    assert health_payload["mode"] == "health"

    unknown_result = server.handle_message({
        "jsonrpc": "2.0",
        "id": 7,
        "method": "tools/call",
        "params": {"name": "not_a_tool", "arguments": {}},
    })
    assert unknown_result["result"]["isError"] is True
    assert server.handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
