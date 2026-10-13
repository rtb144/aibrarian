-- transcript language check results (aibrarian transcripts langcheck) + manual review
CREATE TABLE IF NOT EXISTS aib.transcript_lang (
    id             BIGSERIAL PRIMARY KEY,
    path           TEXT NOT NULL UNIQUE,          -- the .transcript.json (may not exist for source=db)
    media_id       BIGINT REFERENCES aib.media_file(id) ON DELETE SET NULL,
    title          TEXT,
    source         TEXT NOT NULL DEFAULT 'sidecar',
    detected_lang  TEXT,
    confidence     DOUBLE PRECISION,
    stored_lang    TEXT,
    mismatch       BOOLEAN NOT NULL DEFAULT false,
    non_english    BOOLEAN NOT NULL DEFAULT false,
    repetitive     BOOLEAN NOT NULL DEFAULT false,
    repeat_ratio   DOUBLE PRECISION,
    top_line_share DOUBLE PRECISION,
    segments       INT,
    flags          TEXT[] NOT NULL DEFAULT '{}',
    needs_review   BOOLEAN NOT NULL DEFAULT false,
    snippet        TEXT,
    checked_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    review         TEXT CHECK (review IN ('correct','wrong')),  -- correct = really non-English
    review_note    TEXT,
    reviewed_at    TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS transcript_lang_review_idx ON aib.transcript_lang (needs_review, review);
