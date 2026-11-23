# Self-Test and Optional Sample

## `self-test`

- Uses the deterministic synthetic fixture at `assets/demo/demo_manifest.json` strictly as a test fixture.
- Creates a temporary workspace, SQLite canonical, FTS index, profile, smoke probe, and profile export.
- Verifies write, idempotent replay, read, FTS, status, profile export, and cleanup.
- Deletes the temporary workspace after the test. A failed test also attempts cleanup and reports `cleanup_verified`.
- Never reads real TXT, conversations, `.env`, `.secrets`, canonical databases, or production resources.

## Optional `sample`

- Is explicit teaching/display material only.
- Writes a persistent sample database under the root selected by the user.
- Must not be mixed with a real canonical, `self-test`, or a production profile.
- Is not an installation prerequisite.
- Users can delete the sample root independently.

## Hidden `demo`

The old `demo` command is kept only as a hidden compatibility alias. It runs a transient self-test and warns that Demo data is not retained. New workflows use `self-test` or `sample`.
