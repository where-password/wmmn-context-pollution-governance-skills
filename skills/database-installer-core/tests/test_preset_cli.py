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


def _prepare(tmp_path: Path, capsys):
    item = _item(key="mwh_preset_query", content="Preset flow queryable fact.")
    profile, _, store = _apply_profile(tmp_path, [item])
    raw = json.loads(profile.read_text(encoding="utf-8"))
    raw["max_sensitivity"] = "private"
    profile.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    directory, _ = _write_fixture(
        tmp_path,
        [item],
        [(item, "approve", "2026-11-22T10:00:00+08:00")],
    )
    _, plan = _plan(profile, directory, None, capsys=capsys)
    _, applied, _ = _apply(profile, directory, None, plan["required_confirmation_token"], capsys=capsys)
    memory_id = applied["write_results"][0]["memory_id"]
    worker = OutboxWorker(
        store=store,
        profiles=ProfileRegistry(),
        embedding=FakeEmbedding(),
        vector_index=FakeVectorIndex(),
        write_alias="memory_write_alias",
    )
    assert worker.run_once(owner="preset-test")
    return profile, store, memory_id


def test_preset_init_and_short_query_commands(tmp_path: Path, capsys) -> None:
    profile, store, memory_id = _prepare(tmp_path, capsys)
    preset_path = tmp_path / "workspace" / "config" / "personal-preset.json"
    code = db_skill.main([
        "preset", "init", "--profile", str(profile), "--output", str(preset_path), "--json",
    ])
    payload = _json_output(capsys)
    assert code == 0
    assert payload["status"] == "generated"
    assert preset_path.is_file()
    preset_text = preset_path.read_text(encoding="utf-8")
    assert "api_key" not in preset_text.casefold()
    assert json.loads(preset_text)["profile"].endswith("profile.json")

    code = db_skill.main(["search", "--preset", str(preset_path), "--query", "Preset", "--json"])
    payload = _json_output(capsys)
    assert code == 0
    assert payload["result"]["mode"] == "search_only"
    assert payload["result"]["hits"]

    code = db_skill.main([
        "context-pack", "--preset", str(preset_path), "--query", "Preset", "--json",
    ])
    payload = _json_output(capsys)
    assert code == 0
    assert payload["result"]["mode"] == "context_pack"
    assert payload["result"]["current"]

    code = db_skill.main([
        "maintenance", "grants", "--preset", str(preset_path), "--retention-days", "0", "--json",
    ])
    payload = _json_output(capsys)
    assert code == 0
    assert payload["result"]["status"] == "completed"

    code = db_skill.main([
        "get-memory", "--profile", str(profile), "--memory-id", memory_id, "--json",
    ])
    payload = _json_output(capsys)
    assert code == 0
    assert payload["result"]["memory_id"] == memory_id

    with store.read() as connection:
        assert connection.execute("SELECT COUNT(*) FROM access_grants").fetchone()[0] == 0


def test_personal_command_without_profile_or_preset_is_blocked(capsys) -> None:
    code = db_skill.main(["search", "--query", "anything", "--json"])
    payload = _json_output(capsys)
    assert code == 2
    assert payload["error"]["code"] == "preset_blocked"
