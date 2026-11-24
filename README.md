# WMMN 上下文污染治理

**WMMN Context Pollution Governance Skills**

[![tests](https://github.com/where-password/wmmn-context-pollution-governance-skills/actions/workflows/tests.yml/badge.svg)](https://github.com/where-password/wmmn-context-pollution-governance-skills/actions/workflows/tests.yml)
[![MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

**wmmn · 我密码呢**

**把受控对话整理成可审查的知识，再存入本地、可检索、可审计的长期记忆。**

**Clean controlled conversations into reviewable knowledge, then persist it in a local, searchable, auditable memory layer.**

wmmn (`我密码呢`) maintains this public beta for personal, single-machine use. It is not a generic chatbot and it does not replace the user's LLM. The Skills split responsibilities so conversation cleaning, evidence review, database writes, indexing, retrieval, and final answer generation do not collapse into one opaque pipeline.

## Status

| Component | Version | Status | Responsibility |
| --- | --- | --- | --- |
| `conversation-context-cleaner` | 1.5.0 | Working version | Controlled JSONL cleaning, candidate extraction, evidence checks, review, approval, and database handoff |
| `database-installer-core` | 0.6.0 | Working version | SQLite installation, preset, verified handoff ingest, search, Context Pack, MCP, health, backup, restore, and optional Milvus |
| `memory_core_v1` | API 1.4.0 | Working source | Canonical memory contracts, evidence, state transitions, idempotency, Outbox, and retrieval services |
| Milvus Add-on | 0.6.0 optional | Optional / experimental boundary | Real isolated Milvus write, retrieval, metadata checks, and zero-difference reconcile; production aliases are forbidden |

This is a **public beta**. SQLite-only personal use is the primary path. Milvus requires separate test resources and explicit configuration.

## What problem it solves

Most chat exports are noisy: plans change, decisions are revised, assistant guesses are mixed with user decisions, and old facts remain next to current facts. Simply embedding every message creates a searchable dump, not a reliable memory layer.

This project uses this chain:

1. The Cleaner receives one explicitly authorized conversation JSONL file.
2. It extracts evidence-backed candidates and separates current state, superseded history, tasks, open questions, and conflicts.
3. A human or the narrowly scoped auto-approval rule approves only safe user decisions or preferences.
4. The Database Skill independently revalidates the approval artifacts, writes canonical rows to SQLite, and records an Outbox.
5. The Database Skill processes the derived FTS/Milvus index and returns bounded context.
6. The caller's LLM writes the final answer.

The database never invents the final answer. The Cleaner never writes canonical memory.

## 首次使用说明

首次成功加载任一 Skill 后，Agent 会展示一次以下说明；之后不会在每个对话中重复。

> 已读取成功。这个工具包含两个相互独立的 Skill：
>
> - 对话清理：只处理你当前 Agent 中的对话，不处理微信、飞书等普通聊天记录。它会把 Agent 对话整理成更清楚、方便保存和使用的内容。
> - 数据库 Skill：建立并维护本地数据库，用来保存需要长期保留的信息，也可以保存整理后的 Agent 对话，从而相对降低后续对话的上下文污染。完整功能需要 Embedding API、Docker 等基础环境，你的 Agent 可以协助检测、安装和配置；遇到系统权限或重启要求时会提示你处理。
>
> 两个 Skill 可以单独使用：已有数据库时可以只用对话清理；只整理内容时也可以不用数据库 Skill。建议两个一起使用，长期管理效果更完整。你想做什么，直接告诉我就行。
>
> ---
>
> wmmn · 我密码呢
>

这份说明由两个 Skill 共用的首次加载标记控制；GitHub 主页保留完整文本，方便用户随时查看。安装器或 Agent 只应在首次加载时展示它，不得在后续对话中重复。


## Quick start

### 1. Clone

```bash
git clone https://github.com/where-password/wmmn-context-pollution-governance-skills.git
cd wmmn-context-pollution-governance-skills
```

### 2. Run tests

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_all_tests.ps1
```

### 3. Install the Skills into a Codex home

Database:

```powershell
skills\database-installer-core\scripts\install_skill.ps1 -CodexHome "$HOME\.codex"
```

Cleaner:

```powershell
skills\conversation-context-cleaner\scripts\install_skill.ps1 -CodexHome "$HOME\.codex"
```

The installers never copy databases, local workspaces, `.env`, tests, or caches into the installed Skill runtime.

### 4. Clean a controlled export

Use an authorized JSONL snapshot only. Do not paste private data into public Issues.

```powershell
skills\conversation-context-cleaner\scripts\context-cleaner.cmd clean plan --input-root <ROOT> --input <FILE> --workspace-root <WORKSPACE> --workspace-id <ID> --project-id <PROJECT> --source-id <SOURCE> --domain-profile generic --profile-version 1.0.0 --conversation-id <CONVERSATION> --boundary-type conversation_closed --boundary-evidence "authorized local test" --json
```

Apply the exact saved plan with its confirmation token. Then export a handoff, review it, and generate the approved batch.

### 5. Create the database and preset

```powershell
skills\database-installer-core\scripts\memory-db.cmd install plan --workspace-root <DB_WORKSPACE> --workspace-id <ID> --project-id <PROJECT> --domain-profile generic --owner-principal-id <OWNER> --plan-out <PLAN> --json
```

After applying the install plan, create a preset:

```powershell
skills\database-installer-core\scripts\memory-db.cmd preset init --profile <CONNECTION_PROFILE> --json
```

### 6. Persist the handoff

```powershell
skills\database-installer-core\scripts\memory-db.cmd handoff plan --preset <PRESET> --handoff-dir <HANDOFF> --source-id <SOURCE> --project-id <PROJECT> --json
```

Review the plan, then run `handoff apply` with the exact confirmation token.

### 7. Retrieve context

```powershell
skills\database-installer-core\scripts\memory-db.cmd context-pack --preset <PRESET> --query "your question" --json
```

Or start the read-only MCP server:

```powershell
skills\database-installer-core\scripts\memory-db.cmd mcp --preset <PRESET>
```

MCP tools: `context`, `search`, `get_memory`, `health`.

## Stable and optional paths

### SQLite-only

This is the recommended public path:

- no Docker;
- no Milvus;
- no Embedding API required;
- SQLite canonical plus FTS5;
- preset-driven CLI and read-only MCP.

### Milvus Add-on

Milvus is a derived index, never the canonical source.

- use only isolated test/staging collections and aliases;
- production aliases are rejected;
- the final package has been tested against a real local Milvus instance with an isolated collection;
- the current verified real-Milvus test used deterministic local vectors to avoid external Embedding cost;
- a real external Embedding provider remains user-configured and environment-specific.

See `skills/database-installer-core/references/milvus-addon.md` and `references/mcp.md`.

## Safety boundaries

- The Cleaner accepts one explicit input file under one explicit input root.
- No automatic filesystem or chat-file scanning.
- No reading `.env` or `.secrets` by the installed runtime.
- No production canonical writes without the Database Skill's independent checks.
- No production Milvus alias switching.
- No secret values in profiles or reports.
- No automatic final-answer synthesis inside the Skills.
- All destructive-looking database changes are logical state changes or derived-index writes; backups are created before restore/upgrade operations.

## Validation

- Conversation Cleaner: 155 tests.
- Memory Core retained runtime/tests in this public tree: 153 passed, 3 skipped in the default non-Milvus CI path; 155 passed, 1 skipped when an isolated local Milvus test instance is available.
- Database Skill: 45 tests.
- Final cross-Skill acceptance: Cleaner -> database handoff -> SQLite -> Outbox -> real isolated Milvus -> retrieval.
- Real Milvus acceptance: zero-difference reconcile.

The real-Milvus report is kept outside the distributable Skills under `release/` or the release attachment process; tests never require production resources.

## Repository layout

```text
skills/
  conversation-context-cleaner/
  database-installer-core/
memory_core_v1/
scripts/
  run_all_tests.ps1
  package_skills.py
  validate_skills.py
.github/
  workflows/tests.yml
  ISSUE_TEMPLATE/
release/
```

## Community and limitations

- Agent-host compatibility is not universal. Report the host, OS, Python version, and a redacted reproduction.
- Cleaner extraction is deterministic and deliberately conservative; it is not a semantic oracle.
- Context Pack retrieval quality is not represented by a single accuracy score.
- A user LLM may still produce a poor final answer from good retrieval context.
- Remote multi-tenant IAM/OAuth is intentionally out of scope.

See `CONTRIBUTING.md`, `COMMUNITY.md`, `SECURITY.md`, and `DISCLAIMER.md`.

## Visible watermark policy

Each Skill shows `wmmn · 我密码呢` once in its first successful user-facing onboarding output for an installation. Later turns and conversations do not repeat the visible watermark. Machine-readable attribution remains in the onboarding result and state marker.

## Attribution and license

MIT License. See `LICENSE` and `NOTICE.md`.

Copyright (c) 2026 wmmn (where-password).
