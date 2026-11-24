# Release v1.5.0

## Packages

- `conversation-context-cleaner-1.5.0.zip`
  - SHA-256: `1c56220d5db5d906e21af04332076ff4f4fdc2d91904936bf68abd40f2118ee7`
  - Bytes: 151350
- `database-installer-core-0.6.0.zip`
  - SHA-256: `493b2a70e1a5aa48b2cb84ab53e3f44748d533336354db7a94f46e9c82e9167f`
  - Bytes: 208624

## Verified

- Cleaner: 154 tests passed.
- Memory Core public tree: 153 passed, 3 skipped in the default non-Milvus CI path; 155 passed, 1 skipped when an isolated local Milvus test instance is available.
- Database Skill: 44 tests passed.
- Cross-Skill acceptance: PASS.
- Real isolated Milvus: PASS, full reconcile with 0 differences.
- Real Milvus test used deterministic local vectors; no external Embedding API cost.
- Production aliases and existing collections were not modified.

## Evidence

- `release/evidence/CROSS_SKILL_ACCEPTANCE_V1.5.0.json`
- `release/evidence/REAL_MILVUS_ACCEPTANCE_V1.5.0.json`

Attach both ZIPs and their `.sha256` sidecars to GitHub Release tag `v1.5.0`.
