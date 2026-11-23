PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_migrations (
  migration_id TEXT PRIMARY KEY,
  checksum TEXT NOT NULL,
  applied_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS workspaces (
  workspace_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
  project_id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
  name TEXT NOT NULL,
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS domain_profiles (
  profile_id TEXT NOT NULL,
  version TEXT NOT NULL,
  domain TEXT NOT NULL,
  config_json TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  PRIMARY KEY (profile_id, version)
);

CREATE TABLE IF NOT EXISTS sources (
  source_id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
  project_id TEXT NOT NULL REFERENCES projects(project_id),
  visibility TEXT NOT NULL DEFAULT 'workspace' CHECK (visibility IN ('workspace','private')),
  owner_principal_id TEXT,
  source_path TEXT NOT NULL,
  source_sha256 TEXT NOT NULL,
  source_type TEXT NOT NULL,
  sensitivity TEXT NOT NULL DEFAULT 'private'
    CHECK (sensitivity IN ('public','internal','private','secret')),
  created_at INTEGER NOT NULL,
  CHECK (visibility <> 'private' OR owner_principal_id IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS source_units (
  source_unit_id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL REFERENCES sources(source_id),
  parent_unit_id TEXT,
  scope_type TEXT NOT NULL,
  scope_ref TEXT NOT NULL,
  source_unit_hash TEXT NOT NULL,
  text_content TEXT NOT NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  created_at INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_source_units_source_scope
  ON source_units(source_id, scope_type, scope_ref);

CREATE TABLE IF NOT EXISTS memory_items (
  memory_id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
  project_id TEXT NOT NULL REFERENCES projects(project_id),
  task_id TEXT,
  conversation_id TEXT,
  owner_principal_id TEXT NOT NULL,
  domain_profile TEXT NOT NULL,
  profile_version TEXT NOT NULL,
  memory_type TEXT NOT NULL,
  subtype TEXT,
  subject TEXT,
  normalized_subject TEXT NOT NULL DEFAULT '',
  entity_type TEXT,
  entity_id TEXT,
  content TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  content_hash_version TEXT NOT NULL DEFAULT 'v1',
  dedup_policy TEXT NOT NULL DEFAULT 'content_only'
    CHECK (dedup_policy IN ('content_only','no_auto_merge')),
  status TEXT NOT NULL
    CHECK (status IN ('active','superseded','rejected','archived','pending_review')),
  importance TEXT NOT NULL DEFAULT 'medium'
    CHECK (importance IN ('critical','high','medium','low')),
  sensitivity TEXT NOT NULL DEFAULT 'private'
    CHECK (sensitivity IN ('public','internal','private','secret')),
  visibility TEXT NOT NULL DEFAULT 'private'
    CHECK (visibility IN ('private','workspace','shared')),
  effective_from INTEGER,
  effective_to INTEGER,
  source_id TEXT NOT NULL REFERENCES sources(source_id),
  source_unit_id TEXT NOT NULL REFERENCES source_units(source_unit_id),
  source_scope_json TEXT NOT NULL,
  confidence REAL NOT NULL DEFAULT 0.5 CHECK (confidence >= 0 AND confidence <= 1),
  supersedes_id TEXT REFERENCES memory_items(memory_id),
  version INTEGER NOT NULL DEFAULT 1,
  domain_payload_json TEXT NOT NULL DEFAULT '{}',
  idempotency_key TEXT NOT NULL,
  deleted_at INTEGER,
  delete_reason TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  CHECK (visibility <> 'private' OR owner_principal_id IS NOT NULL),
  CHECK (effective_from IS NULL OR effective_to IS NULL OR effective_to >= effective_from)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_idempotency
  ON memory_items(workspace_id, owner_principal_id, idempotency_key);
CREATE UNIQUE INDEX IF NOT EXISTS idx_unique_active_private_content
  ON memory_items(workspace_id, owner_principal_id, domain_profile, content_hash, normalized_subject)
  WHERE status = 'active' AND deleted_at IS NULL AND dedup_policy = 'content_only' AND visibility = 'private';
CREATE UNIQUE INDEX IF NOT EXISTS idx_unique_active_shared_content
  ON memory_items(workspace_id, domain_profile, content_hash, normalized_subject)
  WHERE status = 'active' AND deleted_at IS NULL AND dedup_policy = 'content_only' AND visibility IN ('workspace','shared');
CREATE UNIQUE INDEX IF NOT EXISTS idx_unique_active_supersede
  ON memory_items(supersedes_id)
  WHERE supersedes_id IS NOT NULL AND status = 'active' AND deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_memory_scope
  ON memory_items(workspace_id, project_id, domain_profile, status, sensitivity, visibility, owner_principal_id)
  WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_memory_subject
  ON memory_items(workspace_id, normalized_subject, entity_type)
  WHERE deleted_at IS NULL;

CREATE TABLE IF NOT EXISTS idempotency_records (
  workspace_id TEXT NOT NULL,
  principal_id TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  operation_type TEXT NOT NULL CHECK (operation_type IN ('write','delete','consolidation')),
  request_sha256 TEXT NOT NULL,
  operation_id TEXT NOT NULL,
  result_json TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  PRIMARY KEY (workspace_id, principal_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS memory_evidence (
  evidence_id TEXT PRIMARY KEY,
  memory_id TEXT NOT NULL REFERENCES memory_items(memory_id),
  source_unit_id TEXT NOT NULL REFERENCES source_units(source_unit_id),
  quote TEXT NOT NULL,
  locator_json TEXT NOT NULL DEFAULT '{}',
  evidence_status TEXT NOT NULL
    CHECK (evidence_status IN ('verified','rejected','unresolved')),
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memory_evidence_memory
  ON memory_evidence(memory_id, evidence_status);

CREATE TRIGGER IF NOT EXISTS trg_memory_active_requires_verified_update
BEFORE UPDATE OF status ON memory_items
WHEN NEW.status = 'active' AND NEW.deleted_at IS NULL
  AND NOT EXISTS (
    SELECT 1 FROM memory_evidence
    WHERE memory_id = NEW.memory_id AND evidence_status = 'verified'
  )
BEGIN
  SELECT RAISE(ABORT, 'active memory requires verified evidence');
END;

CREATE TRIGGER IF NOT EXISTS trg_memory_evidence_guard_delete
BEFORE DELETE ON memory_evidence
WHEN OLD.evidence_status = 'verified'
  AND EXISTS (
    SELECT 1 FROM memory_items
    WHERE memory_id = OLD.memory_id AND status = 'active' AND deleted_at IS NULL
  )
  AND NOT EXISTS (
    SELECT 1 FROM memory_evidence
    WHERE memory_id = OLD.memory_id AND evidence_id <> OLD.evidence_id AND evidence_status = 'verified'
  )
BEGIN
  SELECT RAISE(ABORT, 'cannot remove last verified evidence from active memory');
END;

CREATE TABLE IF NOT EXISTS memory_links (
  link_id TEXT PRIMARY KEY,
  from_memory_id TEXT NOT NULL REFERENCES memory_items(memory_id),
  to_memory_id TEXT NOT NULL REFERENCES memory_items(memory_id),
  link_type TEXT NOT NULL,
  confidence REAL NOT NULL DEFAULT 0.5 CHECK (confidence >= 0 AND confidence <= 1),
  created_at INTEGER NOT NULL,
  CHECK (from_memory_id <> to_memory_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_links_unique
  ON memory_links(from_memory_id, to_memory_id, link_type);

CREATE TABLE IF NOT EXISTS task_states (
  task_id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
  project_id TEXT NOT NULL REFERENCES projects(project_id),
  conversation_id TEXT,
  owner_principal_id TEXT NOT NULL,
  status TEXT NOT NULL,
  sensitivity TEXT NOT NULL DEFAULT 'private'
    CHECK (sensitivity IN ('public','internal','private','secret')),
  visibility TEXT NOT NULL DEFAULT 'private'
    CHECK (visibility IN ('private','workspace','shared')),
  state_json TEXT NOT NULL DEFAULT '{}',
  updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS working_memory (
  wm_id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
  project_id TEXT NOT NULL REFERENCES projects(project_id),
  task_id TEXT NOT NULL,
  conversation_id TEXT,
  owner_principal_id TEXT NOT NULL,
  domain_profile TEXT NOT NULL,
  memory_type TEXT NOT NULL,
  subtype TEXT,
  normalized_subject TEXT NOT NULL DEFAULT '',
  content TEXT NOT NULL,
  sensitivity TEXT NOT NULL DEFAULT 'private'
    CHECK (sensitivity IN ('public','internal','private','secret')),
  visibility TEXT NOT NULL DEFAULT 'private'
    CHECK (visibility IN ('private','workspace','shared')),
  confidence REAL NOT NULL DEFAULT 0.5 CHECK (confidence >= 0 AND confidence <= 1),
  promoted_memory_id TEXT,
  ttl_expires_at INTEGER NOT NULL,
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS memory_outbox (
  outbox_id TEXT PRIMARY KEY,
  memory_id TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  target_type TEXT NOT NULL CHECK (target_type IN ('milvus','fts')),
  target_name TEXT NOT NULL,
  operation TEXT NOT NULL CHECK (operation IN ('upsert','delete','reconcile')),
  item_version INTEGER NOT NULL,
  index_schema_version TEXT NOT NULL,
  embedding_model TEXT,
  embedding_dim INTEGER,
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending','claimed','done','poison')),
  lease_owner TEXT,
  lease_expires_at INTEGER,
  retry_count INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  next_retry_at INTEGER NOT NULL,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_outbox_unique_event
  ON memory_outbox(memory_id, target_type, target_name, item_version, index_schema_version, operation);
CREATE INDEX IF NOT EXISTS idx_outbox_claim
  ON memory_outbox(status, next_retry_at, lease_expires_at);

CREATE TABLE IF NOT EXISTS memory_fts_sync_state (
  memory_id TEXT PRIMARY KEY,
  item_version INTEGER NOT NULL,
  deleted INTEGER NOT NULL DEFAULT 0 CHECK (deleted IN (0,1)),
  updated_at INTEGER NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
  memory_id UNINDEXED,
  workspace_id UNINDEXED,
  project_id UNINDEXED,
  task_id UNINDEXED,
  conversation_id UNINDEXED,
  owner_principal_id UNINDEXED,
  domain_profile UNINDEXED,
  memory_type UNINDEXED,
  status UNINDEXED,
  sensitivity UNINDEXED,
  visibility UNINDEXED,
  content,
  tokenize = 'trigram'
);

CREATE TABLE IF NOT EXISTS access_grants (
  grant_id TEXT PRIMARY KEY,
  principal_id TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  scope_json TEXT NOT NULL,
  actions_json TEXT NOT NULL,
  expires_at INTEGER NOT NULL,
  remaining_queries INTEGER,
  purpose TEXT NOT NULL,
  revoked INTEGER NOT NULL DEFAULT 0 CHECK (revoked IN (0,1)),
  revoked_at INTEGER,
  revoked_reason TEXT,
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS token_replay_cache (
  jti TEXT PRIMARY KEY,
  principal_id TEXT NOT NULL,
  grant_id TEXT NOT NULL,
  expires_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_token_replay_expiry ON token_replay_cache(expires_at);

CREATE TABLE IF NOT EXISTS query_audits (
  query_id TEXT PRIMARY KEY,
  principal_id TEXT NOT NULL,
  token_jti TEXT NOT NULL,
  grant_id TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  query_hash TEXT NOT NULL,
  redacted_query TEXT NOT NULL,
  max_sensitivity TEXT NOT NULL,
  filters_json TEXT NOT NULL,
  retrieved_ids_json TEXT NOT NULL,
  answer_summary TEXT NOT NULL,
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_query_audits_principal
  ON query_audits(principal_id, created_at);
