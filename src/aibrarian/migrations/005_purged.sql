-- Quarantined duplicate copies that were permanently deleted by quarantine-purge.
ALTER TABLE aib.media_file ADD COLUMN IF NOT EXISTS purged_at TIMESTAMPTZ;
