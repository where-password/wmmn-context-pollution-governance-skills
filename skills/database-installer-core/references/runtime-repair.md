# Runtime Repair

`scripts/runtime_preflight.py` is intentionally dependency-free. It can still return a machine-readable repair plan if Pydantic or `memory_core` cannot be imported.

The plan distinguishes:

1. Unsupported or missing Python: provide a controlled portable Python 3.11–3.13 path, or ask the user to approve an official installer command.
2. Missing dependencies: create an isolated workspace venv and install exact versions from `assets/runtime/requirements-runtime.lock`.
3. Missing `memory_core`: install a built wheel or an explicitly selected editable source into that same venv.

The plan does not execute commands. `scripts/bootstrap_runtime.ps1` only acts when `-Apply` is supplied. It does not install Docker, modify system Python, or bypass UAC/security-software/reboot decisions.


V1.5 Milvus is optional. The base runtime lock intentionally excludes `pymilvus`; real vector mode uses `assets/runtime/requirements-milvus.lock` only inside the Skill-owned isolated runtime. Fake bootstrap and SQLite-only never require `pymilvus`.
