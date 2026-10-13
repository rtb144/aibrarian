-- book-scan: a file whose metadata extraction / cataloguing timed out or crashed is
-- recorded with status 'error' so resume skips it instead of retrying it forever.
ALTER TABLE aib.suggestion DROP CONSTRAINT IF EXISTS suggestion_status_check;
ALTER TABLE aib.suggestion ADD CONSTRAINT suggestion_status_check
    CHECK (status IN ('open','applied','dismissed','failed','noop','needs_review',
                      'quarantined','undone','quarantined_dst_exists','error'));
