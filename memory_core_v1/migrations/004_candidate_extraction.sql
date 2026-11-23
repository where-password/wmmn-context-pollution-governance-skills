-- Step 3 candidate extraction schema. All writes stay in the isolated Step 3 database.
-- The canonical, Outbox, FTS and Milvus tables are intentionally untouched.

CREATE TABLE IF NOT EXISTS step3_runs (
  run_id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL,
  project_id TEXT NOT NULL,
  source_id TEXT NOT NULL,
  source_revision_id TEXT NOT NULL,
  conversation_id TEXT NOT NULL,
  boundary_type TEXT NOT NULL CHECK (boundary_type IN ('conversation_closed','task_terminal','explicit_scan_boundary')),
  boundary_evidence_json TEXT NOT NULL,
  prompt_version TEXT NOT NULL,
  schema_version TEXT NOT NULL,
  provider TEXT NOT NULL,
  model_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('running','completed','partial','failed','quarantined')),
  extraction_count INTEGER NOT NULL DEFAULT 0,
  cache_hit_count INTEGER NOT NULL DEFAULT 0,
  candidate_count INTEGER NOT NULL DEFAULT 0,
  new_candidate_count INTEGER NOT NULL DEFAULT 0,
  open_question_count INTEGER NOT NULL DEFAULT 0,
  conflict_count INTEGER NOT NULL DEFAULT 0,
  usage_json TEXT NOT NULL DEFAULT '{}',
  started_at INTEGER NOT NULL,
  finished_at INTEGER,
  error_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_step3_runs_scope
  ON step3_runs(workspace_id, project_id, source_id, source_revision_id, started_at);

CREATE TABLE IF NOT EXISTS step3_topic_segments (
  topic_segment_id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL,
  project_id TEXT NOT NULL,
  source_id TEXT NOT NULL,
  source_revision_id TEXT NOT NULL,
  conversation_id TEXT NOT NULL,
  topic_key TEXT NOT NULL,
  start_message_id TEXT NOT NULL,
  end_message_id TEXT NOT NULL,
  message_ids_json TEXT NOT NULL,
  start_sequence INTEGER NOT NULL,
  end_sequence INTEGER NOT NULL,
  summary TEXT NOT NULL,
  dialogue_acts_json TEXT NOT NULL,
  workspace_assignment_status TEXT NOT NULL,
  workspace_assignment_confidence REAL NOT NULL,
  segment_version TEXT NOT NULL,
  boundary_type TEXT NOT NULL,
  boundary_evidence_json TEXT NOT NULL,
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_step3_segments_scope
  ON step3_topic_segments(workspace_id, project_id, source_id, source_revision_id, start_sequence);

CREATE TABLE IF NOT EXISTS step3_extractions (
  extraction_id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL,
  project_id TEXT NOT NULL,
  source_id TEXT NOT NULL,
  source_revision_id TEXT NOT NULL,
  conversation_id TEXT NOT NULL,
  topic_segment_id TEXT NOT NULL,
  chunk_index INTEGER NOT NULL DEFAULT 0,
  boundary_type TEXT NOT NULL,
  boundary_evidence_json TEXT NOT NULL,
  prompt_version TEXT NOT NULL,
  schema_version TEXT NOT NULL,
  provider TEXT NOT NULL,
  model_id TEXT NOT NULL,
  input_sha256 TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('succeeded','failed','running')),
  response_json TEXT NOT NULL DEFAULT '{}',
  response_sha256 TEXT,
  prompt_tokens INTEGER NOT NULL DEFAULT 0,
  completion_tokens INTEGER NOT NULL DEFAULT 0,
  total_tokens INTEGER NOT NULL DEFAULT 0,
  finish_reason TEXT,
  latency_ms INTEGER,
  error_type TEXT,
  error_message TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  UNIQUE(workspace_id, project_id, source_id, source_revision_id, conversation_id,
         topic_segment_id, chunk_index, boundary_type, prompt_version, schema_version, model_id)
);
CREATE INDEX IF NOT EXISTS idx_step3_extractions_cache
  ON step3_extractions(workspace_id, project_id, source_id, source_revision_id, conversation_id, status);

CREATE TABLE IF NOT EXISTS candidate_memories (
  candidate_id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL,
  project_id TEXT NOT NULL,
  conversation_id TEXT NOT NULL,
  topic_segment_id TEXT NOT NULL,
  source_id TEXT NOT NULL,
  source_revision_id TEXT NOT NULL,
  domain_profile TEXT NOT NULL,
  profile_version TEXT NOT NULL,
  candidate_type TEXT,
  memory_type TEXT NOT NULL,
  subtype TEXT,
  content TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  claim_key TEXT,
  assertion_source TEXT NOT NULL,
  verification_state TEXT NOT NULL,
  epistemic_status TEXT NOT NULL,
  authority_basis TEXT NOT NULL,
  confidence REAL NOT NULL,
  importance TEXT NOT NULL,
  declared_sensitivity TEXT NOT NULL,
  requested_visibility TEXT NOT NULL,
  evidence_json TEXT NOT NULL,
  confirmation_links_json TEXT NOT NULL DEFAULT '[]',
  risk_flags_json TEXT NOT NULL DEFAULT '[]',
  conflicts_json TEXT NOT NULL DEFAULT '[]',
  invalidated_by_json TEXT NOT NULL DEFAULT '[]',
  supersedes_candidate_id TEXT,
  workspace_assignment_status TEXT NOT NULL,
  workspace_assignment_confidence REAL NOT NULL,
  review_status TEXT NOT NULL,
  record_status TEXT NOT NULL,
  external_processing_allowed INTEGER NOT NULL DEFAULT 1,
  domain_payload_json TEXT NOT NULL DEFAULT '{}',
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_candidate_memories_scope
  ON candidate_memories(workspace_id, project_id, source_id, source_revision_id, review_status);
CREATE INDEX IF NOT EXISTS idx_candidate_memories_claim
  ON candidate_memories(workspace_id, project_id, claim_key, content_hash);

CREATE TABLE IF NOT EXISTS candidate_review_queue (
  queue_id TEXT PRIMARY KEY,
  candidate_id TEXT NOT NULL UNIQUE,
  workspace_id TEXT NOT NULL,
  project_id TEXT NOT NULL,
  reasons_json TEXT NOT NULL,
  priority TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_candidate_review_scope
  ON candidate_review_queue(workspace_id, project_id, status);

CREATE TABLE IF NOT EXISTS candidate_task_states (
  task_state_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  candidate_id TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  project_id TEXT NOT NULL,
  conversation_id TEXT NOT NULL,
  status TEXT NOT NULL,
  state_json TEXT NOT NULL,
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_candidate_tasks_scope
  ON candidate_task_states(workspace_id, project_id, status);

CREATE TABLE IF NOT EXISTS open_questions (
  question_id TEXT PRIMARY KEY,
  candidate_id TEXT NOT NULL UNIQUE,
  workspace_id TEXT NOT NULL,
  project_id TEXT NOT NULL,
  conversation_id TEXT NOT NULL,
  question_text TEXT NOT NULL,
  evidence_json TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','closed')),
  opened_at INTEGER NOT NULL,
  closed_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_open_questions_scope
  ON open_questions(workspace_id, project_id, status);

CREATE TABLE IF NOT EXISTS candidate_conflicts (
  conflict_id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL,
  project_id TEXT NOT NULL,
  from_candidate_id TEXT NOT NULL,
  to_candidate_id TEXT NOT NULL,
  relation TEXT NOT NULL CHECK (relation IN ('conflicts_with','supersedes','invalidates')),
  status TEXT NOT NULL DEFAULT 'open',
  evidence_json TEXT NOT NULL DEFAULT '[]',
  created_at INTEGER NOT NULL,
  UNIQUE(from_candidate_id, to_candidate_id, relation)
);

CREATE TABLE IF NOT EXISTS step3_llm_requests (
  request_id TEXT PRIMARY KEY,
  extraction_id TEXT NOT NULL,
  run_id TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  project_id TEXT NOT NULL,
  source_id TEXT NOT NULL,
  source_revision_id TEXT NOT NULL,
  conversation_id TEXT NOT NULL,
  topic_segment_id TEXT NOT NULL,
  chunk_index INTEGER NOT NULL,
  provider TEXT NOT NULL,
  model_id TEXT NOT NULL,
  request_sha256 TEXT NOT NULL,
  prompt_tokens INTEGER NOT NULL DEFAULT 0,
  completion_tokens INTEGER NOT NULL DEFAULT 0,
  total_tokens INTEGER NOT NULL DEFAULT 0,
  finish_reason TEXT,
  latency_ms INTEGER,
  error_type TEXT,
  estimated_cost_usd REAL NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_step3_llm_requests_run
  ON step3_llm_requests(run_id, created_at);
