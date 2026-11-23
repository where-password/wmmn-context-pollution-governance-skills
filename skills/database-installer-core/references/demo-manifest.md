# Synthetic Test Fixture

`assets/demo/demo_manifest.json` and `assets/demo/synthetic_memories.jsonl` are retained as the deterministic synthetic fixture for `self-test` and optional `sample`.

Apply verifies:

- manifest schema and version;
- synthetic and redacted flags;
- fixture relative path and SHA-256;
- record count and canonical record hash;
- active/FTS counts after initialization.

The fixture is not a long-lived Demo mode, not a user installation default, and not a source for real user data.
