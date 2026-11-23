-- P0-2 dual-field migration. Legacy rows remain readable and are classified
-- explicitly by a separate, auditable reclassification pass.

ALTER TABLE memory_items
  ADD COLUMN assertion_source TEXT NOT NULL DEFAULT 'legacy_unknown'
  CHECK (assertion_source IN (
    'user_decision',
    'user_preference',
    'user_request',
    'user_report',
    'primary_source',
    'external_document',
    'tool_output',
    'assistant_proposal',
    'assistant_inference',
    'legacy_unknown'
  ));

ALTER TABLE memory_items
  ADD COLUMN verification_state TEXT NOT NULL DEFAULT 'legacy_unclassified'
  CHECK (verification_state IN (
    'not_required',
    'quote_verified',
    'user_confirmed',
    'source_verified',
    'tool_verified',
    'unverified',
    'conflict',
    'legacy_unclassified'
  ));

CREATE INDEX IF NOT EXISTS idx_memory_assertion_verification
  ON memory_items(workspace_id, project_id, assertion_source, verification_state, status)
  WHERE deleted_at IS NULL;
