-- Step 2 raw-only incremental ingestion schema.
-- This migration adds durable run/cursor/revision/quarantine state and makes
-- source_units carry an explicit project scope. It does not create candidates,
-- canonical records, Outbox index events, or Milvus metadata.

ALTER TABLE source_units ADD COLUMN project_id TEXT;
UPDATE source_units
SET project_id = (
  SELECT s.project_id FROM sources s WHERE s.source_id = source_units.source_id
)
WHERE project_id IS NULL;

CREATE TRIGGER IF NOT EXISTS trg_source_units_project_id_insert
BEFORE INSERT ON source_units
FOR EACH ROW
WHEN NEW.project_id IS NULL OR trim(NEW.project_id) = ''
BEGIN
  SELECT RAISE(ABORT, 'source_units.project_id must be explicit and non-empty');
END;

CREATE TRIGGER IF NOT EXISTS trg_source_units_project_id_update
BEFORE UPDATE OF project_id ON source_units
FOR EACH ROW
WHEN NEW.project_id IS NULL OR trim(NEW.project_id) = ''
BEGIN
  SELECT RAISE(ABORT, 'source_units.project_id must be explicit and non-empty');
END;

CREATE INDEX IF NOT EXISTS idx_source_units_project_scope
  ON source_units(project_id, source_id, scope_type, scope_ref);

CREATE TABLE IF NOT EXISTS ingestion_runs (
  run_id TEXT PRIMARY KEY,
  batch_id TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  project_id TEXT NOT NULL,
  source_id TEXT NOT NULL,
  source_revision_id TEXT,
  source_path TEXT NOT NULL,
  source_sha256 TEXT NOT NULL,
  mode TEXT NOT NULL CHECK (mode IN ('dry_run','apply')),
  status TEXT NOT NULL CHECK (status IN ('pending','running','partial','completed','failed','quarantined')),
  started_at INTEGER NOT NULL,
  finished_at INTEGER,
  cursor_before_json TEXT NOT NULL DEFAULT '{}',
  cursor_after_json TEXT NOT NULL DEFAULT '{}',
  summary_json TEXT NOT NULL DEFAULT '{}',
  error_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_ingestion_runs_scope
  ON ingestion_runs(workspace_id, project_id, source_id, started_at);

CREATE TABLE IF NOT EXISTS ingestion_batches (
  batch_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES ingestion_runs(run_id),
  workspace_id TEXT NOT NULL,
  project_id TEXT NOT NULL,
  source_id TEXT NOT NULL,
  source_revision_id TEXT,
  conversation_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('pending','running','partial','completed','failed','quarantined')),
  accepted_count INTEGER NOT NULL DEFAULT 0,
  duplicate_count INTEGER NOT NULL DEFAULT 0,
  quarantine_count INTEGER NOT NULL DEFAULT 0,
  failed_count INTEGER NOT NULL DEFAULT 0,
  attachment_review_count INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL,
  finished_at INTEGER,
  summary_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_ingestion_batches_scope
  ON ingestion_batches(workspace_id, project_id, source_id, conversation_id, created_at);

CREATE TABLE IF NOT EXISTS ingestion_records (
  record_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES ingestion_runs(run_id),
  batch_id TEXT NOT NULL REFERENCES ingestion_batches(batch_id),
  workspace_id TEXT NOT NULL,
  project_id TEXT NOT NULL,
  source_id TEXT NOT NULL,
  source_revision_id TEXT,
  conversation_id TEXT NOT NULL,
  message_id TEXT,
  sequence INTEGER,
  event_type TEXT NOT NULL CHECK (event_type IN ('message','edit','delete')),
  revision_id TEXT,
  source_unit_id TEXT,
  source_unit_hash TEXT,
  raw_line_number INTEGER NOT NULL,
  raw_row_sha256 TEXT NOT NULL,
  record_status TEXT NOT NULL CHECK (record_status IN ('accepted','duplicate','quarantined','rejected','retryable','failed')),
  raw_committed INTEGER NOT NULL DEFAULT 0 CHECK (raw_committed IN (0,1)),
  review_status TEXT NOT NULL DEFAULT 'none',
  error_code TEXT,
  error_message TEXT,
  created_at INTEGER NOT NULL,
  UNIQUE(run_id, raw_line_number)
);
CREATE INDEX IF NOT EXISTS idx_ingestion_records_scope
  ON ingestion_records(workspace_id, project_id, source_id, conversation_id, message_id, sequence);
CREATE INDEX IF NOT EXISTS idx_ingestion_records_run_status
  ON ingestion_records(run_id, record_status, review_status);

CREATE TABLE IF NOT EXISTS ingestion_cursors (
  cursor_id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL,
  project_id TEXT NOT NULL,
  source_id TEXT NOT NULL,
  conversation_id TEXT NOT NULL,
  max_observed_sequence INTEGER NOT NULL DEFAULT -1,
  last_contiguous_committed_sequence INTEGER NOT NULL DEFAULT -1,
  last_committed_message_id TEXT,
  last_committed_source_unit_hash TEXT,
  source_sha256_at_cursor TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('active','partial','quarantined')),
  updated_at INTEGER NOT NULL,
  UNIQUE(workspace_id, project_id, source_id, conversation_id)
);
CREATE INDEX IF NOT EXISTS idx_ingestion_cursors_scope
  ON ingestion_cursors(workspace_id, project_id, source_id, status);

CREATE TABLE IF NOT EXISTS source_revisions (
  source_revision_id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL,
  project_id TEXT NOT NULL,
  source_id TEXT NOT NULL,
  source_path TEXT NOT NULL,
  source_sha256 TEXT NOT NULL,
  source_type TEXT NOT NULL,
  manifest_path TEXT,
  manifest_sha256 TEXT,
  imported_at INTEGER NOT NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  UNIQUE(source_id, source_sha256)
);
CREATE INDEX IF NOT EXISTS idx_source_revisions_scope
  ON source_revisions(workspace_id, project_id, source_id, imported_at);

CREATE TABLE IF NOT EXISTS message_revision_events (
  event_id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL,
  project_id TEXT NOT NULL,
  source_id TEXT NOT NULL,
  conversation_id TEXT NOT NULL,
  message_id TEXT NOT NULL,
  sequence INTEGER NOT NULL,
  revision_id TEXT NOT NULL,
  event_type TEXT NOT NULL CHECK (event_type IN ('message','edit','delete')),
  source_unit_id TEXT,
  source_unit_hash TEXT NOT NULL,
  scope_ref TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  UNIQUE(source_id, conversation_id, message_id, revision_id, event_type)
);
CREATE INDEX IF NOT EXISTS idx_message_revision_events_scope
  ON message_revision_events(workspace_id, project_id, source_id, conversation_id, sequence);
CREATE INDEX IF NOT EXISTS idx_message_revision_events_message
  ON message_revision_events(source_id, conversation_id, message_id, created_at);
