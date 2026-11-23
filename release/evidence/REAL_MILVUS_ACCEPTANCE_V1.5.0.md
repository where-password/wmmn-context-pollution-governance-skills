# Real Milvus Acceptance V1.5.0

Status: PASS

- Collection: `memory_core_final_v1_acceptance`
- Read alias: `memory_read_alias_final_v1_acceptance_test`
- Write alias: `memory_write_alias_final_v1_acceptance_test`
- Milvus count: 2
- Sync: completed
- Query hits: 2
- Reconcile: passed (differences: 0)
- Health: warning only because the health API reports “full 5-tuple reconcile was not performed”; all metadata/count checks passed and a separate full reconcile passed with zero differences.
- Embedding: deterministic local fake (no external API cost)

This test used the final extracted Database Skill package and the restored local Milvus container. It did not modify production aliases or existing collections.
