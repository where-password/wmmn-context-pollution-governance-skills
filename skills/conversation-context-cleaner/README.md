# conversation-context-cleaner

`conversation-context-cleaner` is an independent Codex Skill and the conversation-preprocessing module for the database Skill. It parses explicit, controlled conversation JSONL snapshots into evidence-backed review artifacts, resolved conversation state, and database handoff suggestions.

## Release

- Skill/package version: `1.5.0`
- API version: `1.5.0`
- Contract version: `conversation-context-cleaner-v1.5.0`
- Deterministic extractor: `deterministic-rules-v13`

## Boundaries

- Review-only: candidates, parent groups, task items, noise items, open questions, and review packages do not record approval decisions or promotions.
- Handoff-only: exported handoff items and approval records are proposals for the database Skill; they are not canonical memory.
- Approval-gated: database adapters require an explicit `approve` bound to the handoff and full item hash.
- Revocation-aware: approval revocation blocks unexecuted writes. After a record is written, revoke can only produce a deletion/tombstone intent using an explicit memory ID/version map.
- Database validation remains mandatory: the database Skill must revalidate the ledger, latest decision, item hash, authorization, expected version, and transaction.

This Skill does not invoke an LLM or network service, does not scan directories, does not read `.env` or `.secrets`, and does not write production canonical storage, Milvus, or aliases.

See `SKILL.md` and `references/` for operating instructions, contracts, approval review, and database execution boundaries.

## Personal shortcuts

`clean plan` and `handoff export` accept `--preset <JSON>` with contract `conversation-cleaner-preset-v1`. Presets can supply workspace/input/scope boundaries and conversation allowlists. `handoff review auto-approve` approves only the frozen safe subset: quote-verified explicit user decisions/preferences/requests, with no supersede, conflict, mixed-semantic, assistant-inference, secret, or other risk marker. Everything else remains pending.
