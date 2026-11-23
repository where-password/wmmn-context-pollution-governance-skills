# Security

- Never read `.env` or `.secrets`; configure only environment variable references.
- Doctor reports presence and names only, never values.
- Serialized output is scanned before display.
- Known secret values, Authorization/Bearer headers, credentialed URIs, and canaries block output. A blocked report must not echo or persist the payload.
- A minimal safe receipt may contain only command/check IDs and redaction counts.
- Ordinary SHA-256, UUID, and content hashes are warnings, not automatic secret findings.
- Profiles use `workspace_root` plus relative managed paths; external absolute paths mark the profile `portable=false`.
- No whole-disk scan and no production alias, collection, task, or canonical mutation.


## Milvus V1.5

- Only test/staging aliases are allowed; any `prod` alias is rejected.
- Existing collection/alias conflicts fail closed.
- Bootstrap cleans only resources it created in the current isolated attempt.
- `MILVUS_URI`, `EMBEDDING_API_KEY`, and local MinIO credentials are represented by env refs; values are never written to profile/log/report.
- Fake backend does not use real secrets or network services.
- SQLite canonical remains the only authority; Milvus is a rebuildable derived index.
