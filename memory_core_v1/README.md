# Memory Core

Memory Core is the canonical memory foundation used by the Database Skill.

## Scope

- SQLite canonical records, evidence, versions, supersession, and soft delete.
- Claims, assertion source, verification state, and scope enforcement.
- Idempotent writes and deletes.
- Derived FTS and Outbox events.
- Optional Milvus index synchronization.
- Read-only query, Context Pack, health, and MCP-facing contracts.

## Public API

The supported integration boundary is `memory_core.skill_api`. Internal pipeline modules are not a public contract.

Key APIs include:

- `install_plan` / `install_apply`
- `connect_plan` / `connect_apply`
- `plan_handoff_ingest` / `apply_handoff_ingest_local`
- `resolve_written_memory_map_from_canonical`
- `search_memory`, `query_context`, `build_context_pack`, `get_memory`, `health_check`
- `sync_milvus_outbox`
- `cleanup_local_grants`
- backup, restore, migration, attach, and Milvus bootstrap APIs.

## Tests

From the repository root:

```powershell
$env:PYTHONPATH='memory_core_v1/src'
python -m pytest -q -o addopts="" --basetemp work/pytest-memory memory_core_v1/tests
```

Tests use synthetic isolated SQLite data. Real Milvus tests are opt-in and must use non-production resources.

## Boundaries

- SQLite is the canonical fact source.
- FTS and Milvus are derived indexes.
- Milvus query results must be rechecked against canonical SQLite.
- Production aliases are outside the public test path.
- The caller's LLM writes the final answer.
