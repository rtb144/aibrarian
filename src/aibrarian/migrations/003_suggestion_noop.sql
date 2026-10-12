-- No-op suggestions (target == source, or op 'none') get status 'noop' so they are
-- hidden from the open review queue but still remembered (scans resume past them).
ALTER TABLE aib.suggestion DROP CONSTRAINT IF EXISTS suggestion_status_check;
ALTER TABLE aib.suggestion ADD CONSTRAINT suggestion_status_check
    CHECK (status IN ('open','applied','dismissed','failed','noop'));
UPDATE aib.suggestion SET status='noop', op='none'
 WHERE status='open' AND (op='none' OR target_path IS NULL OR target_path = source_path);
CREATE INDEX IF NOT EXISTS suggestion_status_idx ON aib.suggestion (status, domain);
