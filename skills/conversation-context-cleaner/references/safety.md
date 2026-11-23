# Safety Boundaries

## Production boundary

V1 has no import or call path to canonical databases, SQLite memory stores, Milvus, embeddings, aliases, outbox workers, or business-answer services. All writes are restricted to the caller-supplied isolated workspace and its `conversation-cleaner/v1` subtree.

The Skill does not install a database, create a canonical, promote a candidate, modify an alias, or start a daemon.

## Input boundary

- Require one explicit file under one explicit input root.
- Do not glob, recurse, or auto-discover input.
- Reject any resolved path containing `.env`, `.env.*`, `.secrets`, `.secret`, or `secrets`.
- Reject non-UTF-8 input, oversized input, missing required fields, timestamps without offsets, duplicate IDs/sequences, and sequence gaps.
- Unsupported edit/revision/delete events are blocked in V1 rather than guessed.

## Secret handling

Scan raw input and generated artifacts for marker categories including private keys, bearer credentials, API key/secret/token assignments, synthetic OpenAI keys, password assignments, and credentialed URIs.

When a marker is found:

- block the plan or apply;
- report category and line number only;
- never echo the matched value;
- write no state.

## Review boundary

Every candidate is `suggestion_only=true`. Review stats report `decisions_recorded=0`. Open questions remain `open` and `resolved=false`; V1 has no close-question path.

## LLM and dependency boundary

V1 is standard-library-only and deterministic. It does not call an LLM, embedding API, network client, subprocess, database driver, or production service. The dependency-boundary test rejects imports in those categories.