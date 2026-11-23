-- Step 3 retry and schema audit migration.
-- No canonical, Outbox, FTS, Milvus or alias table is touched.

ALTER TABLE step3_extractions ADD COLUMN raw_response TEXT;
ALTER TABLE step3_extractions ADD COLUMN parsed_json TEXT;
ALTER TABLE step3_extractions ADD COLUMN schema_status TEXT NOT NULL DEFAULT 'unknown';
ALTER TABLE step3_extractions ADD COLUMN cache_hit INTEGER NOT NULL DEFAULT 0;
ALTER TABLE step3_extractions ADD COLUMN normalizations_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE step3_extractions ADD COLUMN segment_status TEXT NOT NULL DEFAULT 'pending';
ALTER TABLE step3_extractions ADD COLUMN retry_of_extraction_id TEXT;
ALTER TABLE step3_extractions ADD COLUMN response_source TEXT NOT NULL DEFAULT 'unknown';

ALTER TABLE step3_llm_requests ADD COLUMN schema_status TEXT NOT NULL DEFAULT 'unknown';
ALTER TABLE step3_llm_requests ADD COLUMN cache_hit INTEGER NOT NULL DEFAULT 0;
ALTER TABLE step3_llm_requests ADD COLUMN normalizations_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE step3_llm_requests ADD COLUMN segment_status TEXT NOT NULL DEFAULT 'pending';
ALTER TABLE step3_llm_requests ADD COLUMN response_source TEXT NOT NULL DEFAULT 'provider_raw';

CREATE TABLE IF NOT EXISTS step3_segment_audits (
  audit_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  project_id TEXT NOT NULL,
  source_id TEXT NOT NULL,
  source_revision_id TEXT NOT NULL,
  conversation_id TEXT NOT NULL,
  topic_segment_id TEXT NOT NULL,
  chunk_index INTEGER NOT NULL,
  cache_hit INTEGER NOT NULL DEFAULT 0,
  schema_status TEXT NOT NULL,
  finish_reason TEXT,
  prompt_tokens INTEGER NOT NULL DEFAULT 0,
  completion_tokens INTEGER NOT NULL DEFAULT 0,
  total_tokens INTEGER NOT NULL DEFAULT 0,
  error_type TEXT,
  raw_response TEXT NOT NULL,
  parsed_json TEXT NOT NULL,
  normalizations_json TEXT NOT NULL DEFAULT '[]',
  segment_status TEXT NOT NULL,
  response_source TEXT NOT NULL,
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_step3_segment_audits_run
  ON step3_segment_audits(run_id, topic_segment_id, chunk_index);
