# V1.5 Milvus Add-on

## Modes

1. SQLite-only: `self-test`, `install`, `connect`, `status`, `backup`, `restore`, `upgrade` work without Docker/Milvus/Embedding.
2. Existing Milvus: provide a route file whose canonical DB matches the SQLite profile and whose aliases are test/staging names. `milvus connect plan/apply` only writes a local Milvus profile.
3. Local Milvus: provide a workspace root, review `milvus install plan`, then apply with the exact token. The fixed Compose file uses `milvusdb/milvus:v2.6.27`. Docker must already be available; the Skill never installs it.

## Commands

- `milvus status --profile <PROFILE> [--route-file <ROUTE>] [--fake]`
- `milvus connect plan --profile <PROFILE> --route-file <ROUTE> [--fake] --plan-out <PLAN>`
- `milvus connect apply --plan-file <PLAN> --confirm-token <TOKEN>`
- `milvus install plan --workspace-root <ROOT> --plan-out <PLAN>`
- `milvus install apply --plan-file <PLAN> --confirm-token <TOKEN>`
- `milvus bootstrap plan --profile <PROFILE> --route-file <ROUTE> --backend fake|real --plan-out <PLAN>`
- `milvus bootstrap apply --profile <PROFILE> --plan-file <PLAN> --confirm-token <TOKEN> --backend fake|real`

## Safety

- Test/staging aliases only; any `prod` name is rejected.
- Existing collection/alias conflicts are blocked.
- Bootstrap creates only the isolated resources named in the plan and cleans them on failure.
- Fake backend performs deterministic vector indexing/reconcile without network calls.
- Real backend requires `MILVUS_URI`, Embedding env refs, and `pymilvus`; install the optional pinned dependency file only inside the isolated runtime.
- Base SQLite install does not install `requirements-milvus.lock`.
- Reconcile acceptance requires zero differences against canonical.
- A production alias is never created, bound, or switched.
