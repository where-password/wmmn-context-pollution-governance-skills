# Context Auto Routing

`context-auto` lets the caller ask a natural-language question without knowing project IDs. The router selects the operational conversation project, the course project, or both, then returns one labeled context pack per selected route.

## Policy file

The policy is a UTF-8 JSON file with `contract_version: context-auto-routes-v1`. Each route declares a stable route ID, a user-facing source label, a project ID, an optional `domain_profile` override, strong regex patterns (weight 3), weak regex patterns (weight 1), and a threshold. `default_route_id` is used when no route crosses its threshold. The example is `assets/routing/example-context-routes.json`.

## Safety behavior

- Routing is deterministic and local; it does not call an LLM or write the database.
- A route whose project is not present in the connection profile is skipped with a warning.
- Multi-project profiles require a route file; a single-project profile can use the safe default route.
- `context-auto` is read-only. It never promotes candidates, changes canonical state, or writes Milvus.
- Source labels must remain visible when the caller combines packs.

## Recommended policy

- Explicit course terms such as `100集`, `课程`, `老师`, or `第N集` select the course project.
- Operational context terms such as `当前`, `域名`, `服务器`, `授权`, or `部署` select the operational project.
- Ambiguous tool/technology questions can match both projects.
- Explicit mixed-intent phrases select both projects.
- If no route matches, the configured default route is used; no database write occurs.
