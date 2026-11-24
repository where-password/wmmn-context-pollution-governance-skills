# Changelog

## Unreleased

- Added a one-time first-load guide for the two independent Skills and linked it from the repository homepage.
- Rebranded the public product as `WMMN 上下文污染治理` with the one-time `wmmn · 我密码呢` watermark.

## v1.5.1

- Fixed Windows checkout byte-instability for content-addressed demo fixtures.
- Made platform-sensitive installer and delivery-script tests assert or skip according to the documented Windows-only contract.
- Canonicalized test path comparisons so Windows short-path aliases cannot create false failures.
- Updated GitHub Actions runtime versions and corrected the documented test-count baseline.
- Distributable Skill payloads are unchanged from v1.5.0.

## v1.5.0

- Added scoped read-only search, context, Context Pack, MCP, and health.
- Added SQLite Outbox to real isolated Milvus sync and reconcile verification.
- Added personal presets and retained grant cleanup.
- Added controlled auto-approval for quote-verified explicit user decisions.
- Trimmed historical pipeline modules from the Database Skill runtime.
- Added final cross-Skill acceptance and real isolated Milvus acceptance.

## Earlier development

The project evolved through deterministic conversation extraction, evidence-backed state resolution, review-only handoff packages, local ephemeral grants, SQLite canonical storage, FTS, and optional Milvus indexing.
