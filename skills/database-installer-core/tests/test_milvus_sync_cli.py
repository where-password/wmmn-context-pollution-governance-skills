from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import db_skill
from memory_core.skill_api import DatabaseProfile, SkillBackend

from test_handoff_apply_cli import _apply, _apply_profile, _item, _plan, _write_fixture

READ_ALIAS = "memory_read_alias_cli_sync_test_001"
WRITE_ALIAS = "memory_write_alias_cli_sync_test_001"
COLLECTION = "memory_core_cli_sync_test_v1"


def _json_output(capsys) -> dict:
    return json.loads(capsys.readouterr().out)


def _write_milvus_profile(profile, canonical_db: Path) -> Path:
    raw = json.loads(profile.read_text(encoding="utf-8"))
    milvus_profile = DatabaseProfile(
        environment="test",
        backend=SkillBackend.MILVUS,
        capabilities=("query", "health", "backup", "write"),
        workspace_id=raw["workspace_id"],
        project_ids=tuple(raw["project_ids"]),
        domain_profile=raw["domain_profile"],
        owner_principal_id=raw["owner_principal_id"],
        allowed_visibilities=tuple(raw.get("allowed_visibilities", ["private", "workspace", "shared"])),
        max_sensitivity=raw.get("max_sensitivity", "private"),
        canonical_db=canonical_db,
        canonical_sha256="test-only",
        read_alias=READ_ALIAS,
        write_alias=WRITE_ALIAS,
        physical_collection=COLLECTION,
        embedding_model="BAAI/bge-m3",
        embedding_dimension=1024,
        index_schema_version="memory-index-v1",
        metric_type="COSINE",
        plan_id="milvus-sync-cli-test",
        created_at=datetime.now(timezone.utc),
    )
    target = Path(raw["workspace_root"]) / "config" / "milvus-profile.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(milvus_profile.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return target


def test_milvus_sync_cli_processes_pending_outbox_with_fake_backend(tmp_path: Path, capsys) -> None:
    item = _item(key="mwh_milvus_sync_cli", content="Milvus sync CLI isolated fact.")
    profile, canonical_db, store = _apply_profile(tmp_path, [item])
    raw = json.loads(profile.read_text(encoding="utf-8"))
    raw["max_sensitivity"] = "private"
    profile.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    milvus_profile = _write_milvus_profile(profile, canonical_db)

    directory, _ = _write_fixture(
        tmp_path,
        [item],
        [(item, "approve", "2026-11-22T10:00:00+08:00")],
    )
    _, plan = _plan(profile, directory, None, capsys=capsys)
    _, applied, _ = _apply(profile, directory, None, plan["required_confirmation_token"], capsys=capsys)
    assert applied["status"] == "completed"

    code = db_skill.main([
        "milvus", "sync", "--profile", str(profile), "--fake", "--json",
    ])
    payload = _json_output(capsys)
    assert code == 0
    assert payload["result"]["status"] == "completed"
    assert payload["result"]["mode"] == "fake"
    assert payload["result"]["physical_collection"] == COLLECTION
    assert payload["result"]["outbox_before"] == {"pending": 2}
    assert payload["result"]["outbox_after"] == {"done": 2}
    assert payload["result"]["processed"].get("vector_synced") == 1
    assert payload["result"]["embedding_calls"] == 1
    assert milvus_profile.is_file()

    code = db_skill.main([
        "milvus", "sync", "--profile", str(profile), "--fake", "--json",
    ])
    payload = _json_output(capsys)
    assert code == 0
    assert payload["result"]["status"] == "nothing_to_do"
    with store.read() as connection:
        statuses = {
            row["status"]: int(row["count"])
            for row in connection.execute(
                "SELECT status,COUNT(*) AS count FROM memory_outbox GROUP BY status"
            )
        }
    assert statuses == {"done": 2}
