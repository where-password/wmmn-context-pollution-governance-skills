# Installation

## Recommended path

1. Run `python scripts/runtime_preflight.py` or `doctor` in an environment with a supported interpreter.
2. If Python or dependencies are missing, review the repair plan. Do not let the Skill silently modify system Python.
3. A user who explicitly chooses to proceed can run `scripts/bootstrap_runtime.ps1`.
   - Without `-Apply`, it only prints commands.
   - With `-Apply`, it creates a Skill-owned `<workspace>/.runtime/venv` and installs the exact lock file.
   - Pass `-MemoryCoreSource <wheel-or-source>` so the isolated venv can import the versioned `memory_core.skill_api`.
4. Run `scripts/Start-SQLite-Test.ps1 -Python <PYTHON>` or `scripts/run_tests.ps1 -Python <PYTHON>` from the extracted package; both use packaged `vendor/memory_core` and `vendor/migrations`.
5. Run `scripts/memory-db.cmd doctor` or `python scripts/db_skill.py doctor` against the target workspace.
6. Use `install` for a new database, `connect` for an existing canonical, and `self-test` for a transient end-to-end check.
7. Use `restore` or `upgrade` only after reviewing the corresponding plan and confirmation token.
8. For manual workspace development, set `MEMORY_CORE_SRC` to the `memory_core_v1/src` directory or install `memory_core` as an editable package. The Skill does not hardcode a sibling source path. Extracted packages use `vendor/` first when present.

## V1.5 local Milvus

1. Review `assets/milvus/docker-compose.yml`; it pins Milvus `v2.6.27`, etcd and MinIO images.
2. Ensure Docker engine/Compose is already working and provide `MILVUS_MINIO_USER` / `MILVUS_MINIO_PASSWORD` env refs. The Skill does not install Docker.
3. Run `milvus install plan` and apply only with the exact token.
4. Start with a test route containing `_test_` or `_staging_` aliases.
5. Run `milvus bootstrap plan/apply`; use `--backend fake` for deterministic no-network testing and `--backend real` only against a safe non-production Milvus.
6. Real bootstrap requires `MILVUS_URI` and Embedding env refs. Secret values are not printed.

## Boundaries

- No Docker or Milvus installation.
- No system-Python package installation.
- No UAC, BIOS, reboot, or security-software automation.
- `-InstallPython -Apply` is the explicit controlled winget path; after it completes, the script re-probes Python and reports a new-terminal/PATH/UAC next step if needed.
- No production canonical, collection, alias, or scheduled-task mutation.


## Runtime-only Skill install

Run `scripts/install_skill.ps1 -CodexHome <CODEX_HOME>`. It copies only `SKILL.md`, `agents`, `scripts`, `references`, `assets`, `pyproject.toml` and `.gitignore`; it does not copy `work`, `tests`, `.runtime`, `data`, `config`, backups, `.env`, or `.secrets`. Use `-Force` only after reviewing an existing target. `scripts/uninstall_skill.ps1` removes only known runtime items and preserves unknown user data.
