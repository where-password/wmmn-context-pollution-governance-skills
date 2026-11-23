# SQLite Query Semantics

## Short Chinese queries

For a stripped query of one or two CJK characters, the search gateway uses a restricted SQLite substring fallback over the synchronized FTS content. This handles terms such as `巡检` matching `设备巡检`.

The fallback query applies these filters in SQL before returning candidates:

- workspace and optional `global_kb` visibility;
- project;
- domain profile;
- status;
- maximum sensitivity;
- private-record owner;
- optional chapter filter.

It does not turn the fallback into an unrestricted full-table scan. The normal search gateway performs a second record-level authorization check before constructing hits.

Queries of three or more characters continue to use FTS5 trigram search.

## Packaging

The extracted package contains:

- `vendor/memory_core`: the Python memory core runtime;
- `vendor/migrations`: SQL migrations used by the package;
- `scripts/Start-SQLite-Test.ps1`: package-local transient self-test;
- `scripts/run_tests.ps1`: wrapper that calls the package-local self-test.

The package self-test does not depend on the original workspace path. Python 3.11–3.13 and Pydantic remain environment prerequisites.

## Interface contracts

- `search` returns `search_only` ranked hits. It is retrieval, not answer synthesis.
- `context` returns `context_only` hits and warnings for a caller-owned LLM.
- `get-memory` returns `memory_detail` for one active record and hides evidence quotes unless explicitly requested.
- `health` returns independent consistency, quality, and freshness dimensions.

Both installer connection profiles and attached skill profiles are accepted. Relative canonical paths are resolved under `workspace_root`; parent traversal, symlink escape, unsupported contracts, and profiles without the required capability fail closed. Workspace, project, domain, allowed visibility, sensitivity, and private-owner filters are applied before rows are returned.

## Context Pack

`context-pack` is the bounded injection contract. Default budget is 3,500 tokens; the explicit deep peak is 8,000. It prioritizes current active memory, then same-claim conflicts, then superseded history. Every included entry carries citation and verification metadata. Critical current entries fail closed if they cannot fit; lower-priority history is omitted before active context.
