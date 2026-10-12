-- Unified, durable log of automatic library actions (books first; domain column for more).
CREATE TABLE IF NOT EXISTS aib.library_action (
    id             BIGSERIAL PRIMARY KEY,
    ts             TIMESTAMPTZ NOT NULL DEFAULT now(),
    run_id         TEXT,
    action         TEXT NOT NULL CHECK (action IN ('rename','move','quarantine','skip',
                       'needs_review','error','undo','folder_removed')),
    domain         TEXT NOT NULL DEFAULT 'book',
    suggestion_id  BIGINT,
    old_path       TEXT,
    new_path       TEXT,
    title          TEXT,
    author         TEXT,
    series         TEXT,
    series_index   TEXT,
    confidence     DOUBLE PRECISION,
    reason         TEXT,
    source         TEXT,            -- 'cli' or 'prefect:<flow run id>'
    details        JSONB NOT NULL DEFAULT '{}'::jsonb,  -- moves, manifest, batch (for undo)
    undone_by      BIGINT
);
CREATE INDEX IF NOT EXISTS library_action_ts_idx ON aib.library_action (ts DESC);
CREATE INDEX IF NOT EXISTS library_action_run_idx ON aib.library_action (run_id);

-- Suggestion states used by book-autoapply.
ALTER TABLE aib.suggestion DROP CONSTRAINT IF EXISTS suggestion_status_check;
ALTER TABLE aib.suggestion ADD CONSTRAINT suggestion_status_check
    CHECK (status IN ('open','applied','dismissed','failed','noop','needs_review',
                      'quarantined','undone'));
