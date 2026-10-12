-- Full-file content hashes for the duplicates report (cached by path+size+mtime).
CREATE TABLE IF NOT EXISTS aib.file_hash (
    path        TEXT PRIMARY KEY,
    size_bytes  BIGINT NOT NULL,
    mtime       DOUBLE PRECISION NOT NULL,
    sample      TEXT,            -- head+tail content hash (cheap pre-filter)
    sha256      TEXT,            -- full streamed hash (only for size+sample collisions)
    computed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
