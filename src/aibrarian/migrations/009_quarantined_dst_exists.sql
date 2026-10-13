-- apply: destination exists -> source quarantined (see quarantine.quarantine_dst_exists)
ALTER TABLE aib.suggestion DROP CONSTRAINT IF EXISTS suggestion_status_check;
ALTER TABLE aib.suggestion ADD CONSTRAINT suggestion_status_check
    CHECK (status IN ('open','applied','dismissed','failed','noop','needs_review',
                      'quarantined','undone','quarantined_dst_exists'));
