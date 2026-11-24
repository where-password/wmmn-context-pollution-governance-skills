# Release v1.5.1

This is a CI and release-integrity patch. The two Skill ZIP payloads are unchanged from v1.5.0.

## Packages

- `conversation-context-cleaner-1.5.0.zip`
  - SHA-256: `1c56220d5db5d906e21af04332076ff4f4fdc2d91904936bf68abd40f2118ee7`
  - Bytes: 151350
- `database-installer-core-0.6.0.zip`
  - SHA-256: `493b2a70e1a5aa48b2cb84ab53e3f44748d533336354db7a94f46e9c82e9167f`
  - Bytes: 208624

## Fixed

- Locked line endings for content-addressed JSON/JSONL fixtures, preventing Windows checkout hash mismatches.
- Updated platform-sensitive doctor and delivery-script tests to follow the documented Windows-only contract.
- Canonicalized Windows test paths to avoid short-path alias false failures.
- Updated GitHub Actions action runtimes.
- Clarified default CI versus optional isolated-Milvus test counts.

## Verified

- Cleaner: 154 tests passed.
- Memory Core: 153 passed, 3 skipped without isolated Milvus; 155 passed, 1 skipped with isolated local Milvus.
- Database Skill: 44 tests passed.
- Cross-Skill acceptance: PASS.
- Real isolated Milvus: PASS, full reconcile with 0 differences.
- Production aliases and existing collections were not modified.

Attach both ZIPs and their `.sha256` sidecars to GitHub Release tag `v1.5.1`.
