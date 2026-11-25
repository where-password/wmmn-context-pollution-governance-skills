from __future__ import annotations

import json
from pathlib import Path

import db_skill
from memory_core.fakes import FakeEmbedding, FakeVectorIndex
from memory_core.outbox import OutboxWorker
from memory_core.profiles import ProfileRegistry

from test_handoff_apply_cli import _apply, _apply_profile, _item, _plan, _write_fixture


def _json_output(capsys) -> dict:
    return json.loads(capsys.readouterr().out)


def test_search_context_get_memory_and_health_use_connection_profile(tmp_path: Path, capsys) -> None:
    item = _item(key="mwh_query_cli_1", content="Synthetic queryable memory for the retrieval CLI.")
    profile, _, store = _apply_profile(tmp_path, [item])
    profile_data = json.loads(profile.read_text(encoding="utf-8"))
    profile_data["max_sensitivity"] = "private"
    profile.write_text(json.dumps(profile_data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    directory, _ = _write_fixture(
        tmp_path,
        [item],
        [(item, "approve", "2026-11-22T10:00:00+08:00")],
    )
    _, plan = _plan(profile, directory, None, capsys=capsys)
    _, applied, _ = _apply(
        profile,
        directory,
        None,
        plan["required_confirmation_token"],
        capsys=capsys,
    )
    memory_id = applied["write_results"][0]["memory_id"]
    worker = OutboxWorker(
        store=store,
        profiles=ProfileRegistry(),
        embedding=FakeEmbedding(),
        vector_index=FakeVectorIndex(),
        write_alias="memory_write_alias",
    )
    assert worker.run_once(owner="query-cli-test")

    code = db_skill.main([
        "search", "--profile", str(profile), "--query", "Synthetic", "--top-k", "5", "--json",
    ])
    payload = _json_output(capsys)
    assert code == 0
    assert payload["result"]["mode"] == "search_only"
    assert payload["result"]["hits"]
    assert "memory_id" not in payload["result"]["hits"][0]

    code = db_skill.main([
        "context", "--profile", str(profile), "--query", "Synthetic", "--top-k", "5", "--json",
    ])
    payload = _json_output(capsys)
    assert code == 0
    assert payload["result"]["mode"] == "context_only"
    assert payload["result"]["hits"]

    code = db_skill.main([
        "context-pack", "--profile", str(profile), "--query", "Synthetic",
        "--token-budget", "3000", "--json",
    ])
    payload = _json_output(capsys)
    assert code == 0
    assert payload["result"]["mode"] == "context_pack"
    assert payload["result"]["estimated_tokens"] <= 3000
    assert payload["result"]["current"]

    code = db_skill.main([
        "context-auto", "--profile", str(profile), "--query", "Synthetic",
        "--token-budget", "3000", "--json",
    ])
    payload = _json_output(capsys)
    assert code == 0
    assert payload["result"]["mode"] == "context_auto"
    assert payload["result"]["packs"][0]["source_label"] == "本地记忆"
    assert payload["result"]["packs"][0]["context_pack"]["current"]

    code = db_skill.main([
        "get-memory", "--profile", str(profile), "--memory-id", memory_id, "--json",
    ])
    payload = _json_output(capsys)
    assert code == 0
    assert payload["result"]["mode"] == "memory_detail"
    assert payload["result"]["memory_id"] == memory_id
    assert payload["result"]["evidences"]
    assert all("quote" not in evidence for evidence in payload["result"]["evidences"])

    code = db_skill.main([
        "get-memory", "--profile", str(profile), "--memory-id", memory_id,
        "--include-evidence-quote", "--json",
    ])
    payload = _json_output(capsys)
    assert code == 0
    assert any(evidence.get("quote") for evidence in payload["result"]["evidences"])

    code = db_skill.main(["health", "--profile", str(profile), "--json"])
    payload = _json_output(capsys)
    assert code == 0
    assert payload["result"]["status"] in {"ok", "warning"}
    assert payload["result"]["counts"]["active"] == 1
    assert payload["result"]["checks"]["canonical_exists"] is True


def test_query_cli_is_blocked_when_connection_profile_disables_fts(tmp_path: Path, capsys) -> None:
    item = _item(key="mwh_query_cli_no_fts")
    profile, _, _ = _apply_profile(tmp_path, [item])
    raw = json.loads(profile.read_text(encoding="utf-8"))
    raw["capabilities"]["fts"] = "unavailable"
    profile.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    code = db_skill.main([
        "search", "--profile", str(profile), "--query", "Synthetic", "--json",
    ])
    payload = _json_output(capsys)
    assert code == 2
    assert payload["status"] == "blocked"
    assert "capabilities do not allow query" in payload["error"]["message"]
