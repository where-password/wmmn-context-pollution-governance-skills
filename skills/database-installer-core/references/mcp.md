# Read-only MCP Server

Launch:

`scripts\memory-db.cmd mcp --profile <PROFILE> [--project-id <PROJECT>]`

Transport: newline-delimited JSON-RPC 2.0 over stdio. Protocol responses go to stdout; diagnostics, if any, belong on stderr.

## Tools

| Tool | Inputs | Output |
|---|---|---|
| `context` | `query`, optional `token_budget`, `project_id`, `max_sensitivity` | Bounded `context_pack` |
| `search` | `query`, optional `top_k`, `project_id`, `max_sensitivity` | `search_only` hits without internal IDs |
| `get_memory` | `memory_id`, optional `include_evidence_quote`, scope fields | One `memory_detail` |
| `health` | optional `project_id` | `health` diagnostics |

The server exposes no write, install, backup, restore, upgrade, Milvus administration, prompt, or final-answer tools. Tool failures return `isError=true` with a structured text payload and never print secret values.
