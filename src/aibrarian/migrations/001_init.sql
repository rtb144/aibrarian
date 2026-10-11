-- aibrarian catalog schema v1. All tables live in schema "aib".
CREATE SCHEMA IF NOT EXISTS aib;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS aib.media_file (
    id            BIGSERIAL PRIMARY KEY,
    path          TEXT NOT NULL UNIQUE,
    root          TEXT NOT NULL,
    kind          TEXT NOT NULL CHECK (kind IN ('video','audio','book','other')),
    size_bytes    BIGINT NOT NULL,
    mtime         DOUBLE PRECISION NOT NULL,
    fingerprint   TEXT NOT NULL,
    title         TEXT,
    year          INT,
    season        INT,
    episode       INT,
    jellyfin_id   TEXT,
    missing       BOOLEAN NOT NULL DEFAULT FALSE,
    duration_s    DOUBLE PRECISION,
    probe         JSONB,
    audio_path    TEXT,
    -- pipeline state machine (see docs/status-model.md)
    stage         TEXT NOT NULL DEFAULT 'discovered' CHECK (stage IN (
                    'discovered','probed','audio_extracted','transcribed','embedded',
                    'cataloged','failed','skipped','needs_review')),
    stage_attempts INT NOT NULL DEFAULT 0,
    last_error    TEXT,
    failed_stage  TEXT,
    locked_by     TEXT,
    locked_at     TIMESTAMPTZ,
    stage_changed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    first_seen    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS media_file_root_idx ON aib.media_file (root);
CREATE INDEX IF NOT EXISTS media_file_title_trgm ON aib.media_file USING GIN (title gin_trgm_ops);
CREATE INDEX IF NOT EXISTS media_file_stage_idx ON aib.media_file (stage) WHERE NOT missing;

-- Append-only history of every action attempt / stage transition.
CREATE TABLE IF NOT EXISTS aib.media_event (
    id             BIGSERIAL PRIMARY KEY,
    media_id       BIGINT NOT NULL REFERENCES aib.media_file(id) ON DELETE CASCADE,
    action         TEXT NOT NULL,
    from_stage     TEXT,
    to_stage       TEXT NOT NULL,
    outcome        TEXT NOT NULL CHECK (outcome IN ('ok','failed','skipped','needs_review','reset')),
    attempt        INT NOT NULL DEFAULT 1,
    started_at     TIMESTAMPTZ NOT NULL,
    finished_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    duration_ms    BIGINT GENERATED ALWAYS AS
                     ((EXTRACT(EPOCH FROM (finished_at - started_at)) * 1000)::BIGINT) STORED,
    error          TEXT,
    details        JSONB NOT NULL DEFAULT '{}'::jsonb,
    flow_run_id    TEXT,
    task_run_id    TEXT
);
CREATE INDEX IF NOT EXISTS media_event_media_idx ON aib.media_event (media_id, id);
CREATE INDEX IF NOT EXISTS media_event_time_idx ON aib.media_event (finished_at);

CREATE TABLE IF NOT EXISTS aib.transcript (
    media_id      BIGINT PRIMARY KEY REFERENCES aib.media_file(id) ON DELETE CASCADE,
    fingerprint   TEXT NOT NULL,
    language      TEXT,
    model         TEXT,
    duration_s    DOUBLE PRECISION,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS aib.segment (
    id            BIGSERIAL PRIMARY KEY,
    media_id      BIGINT NOT NULL REFERENCES aib.media_file(id) ON DELETE CASCADE,
    seq           INT NOT NULL,
    start_s       DOUBLE PRECISION NOT NULL,
    end_s         DOUBLE PRECISION NOT NULL,
    text          TEXT NOT NULL,
    words         JSONB,  -- [[start,end,word],...] word-level timings for clipping
    tsv           TSVECTOR GENERATED ALWAYS AS (to_tsvector('simple', text)) STORED,
    UNIQUE (media_id, seq)
);
CREATE INDEX IF NOT EXISTS segment_tsv_idx ON aib.segment USING GIN (tsv);
CREATE INDEX IF NOT EXISTS segment_text_trgm ON aib.segment USING GIN (text gin_trgm_ops);

-- One row per librarian scan (replaces report_<run_id>.json + Neo4j ScanRun).
CREATE TABLE IF NOT EXISTS aib.scan_run (
    id            BIGSERIAL PRIMARY KEY,
    domain        TEXT NOT NULL CHECK (domain IN ('video','book')),
    started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at   TIMESTAMPTZ,
    params        JSONB NOT NULL DEFAULT '{}'::jsonb,
    summary       JSONB NOT NULL DEFAULT '{}'::jsonb,
    flow_run_id   TEXT
);

CREATE TABLE IF NOT EXISTS aib.suggestion (
    id            BIGSERIAL PRIMARY KEY,
    run_id        BIGINT REFERENCES aib.scan_run(id) ON DELETE SET NULL,
    media_kind    TEXT,          -- movie | tv | music | ebook | audiobook | podcast | other
    op            TEXT NOT NULL DEFAULT 'move' CHECK (op IN ('move','rename','none')),
    domain        TEXT NOT NULL CHECK (domain IN ('video','book')),
    source_path   TEXT NOT NULL,
    target_path   TEXT,
    metadata      JSONB NOT NULL DEFAULT '{}'::jsonb,
    confidence    DOUBLE PRECISION NOT NULL,
    decision      TEXT NOT NULL CHECK (decision IN ('auto','suggest','review','reject')),
    status        TEXT NOT NULL DEFAULT 'open'
                  CHECK (status IN ('open','applied','dismissed','failed')),
    rationale     TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (domain, source_path)
);

-- Audit log of every applied/planned file operation (replaces move_log/rename_log JSONL).
CREATE TABLE IF NOT EXISTS aib.file_op (
    id            BIGSERIAL PRIMARY KEY,
    suggestion_id BIGINT REFERENCES aib.suggestion(id) ON DELETE SET NULL,
    op            TEXT NOT NULL,          -- move | rename | sidecar | delete
    src           TEXT NOT NULL,
    dst           TEXT,
    result        TEXT NOT NULL,          -- ok | dry_run | skip_dst_exists | skip_missing | error
    error         TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS file_op_src_idx ON aib.file_op (src);

CREATE TABLE IF NOT EXISTS aib.clip (
    id            BIGSERIAL PRIMARY KEY,
    media_id      BIGINT NOT NULL REFERENCES aib.media_file(id) ON DELETE CASCADE,
    quote         TEXT NOT NULL,
    start_s       DOUBLE PRECISION NOT NULL,
    end_s         DOUBLE PRECISION NOT NULL,
    output_path   TEXT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Open Library editions (replaces openlibrary DB ol_records + DuckDB openlibrary_catalog).
CREATE TABLE IF NOT EXISTS aib.ol_edition (
    key           TEXT PRIMARY KEY,
    title         TEXT NOT NULL,
    title_norm    TEXT NOT NULL,
    authors       TEXT[] NOT NULL DEFAULT '{}',
    isbn10        TEXT,
    isbn13        TEXT,
    publish_year  INT,
    publishers    TEXT[] NOT NULL DEFAULT '{}',
    data          JSONB
);
CREATE INDEX IF NOT EXISTS ol_edition_title_trgm ON aib.ol_edition USING GIN (title_norm gin_trgm_ops);
CREATE INDEX IF NOT EXISTS ol_edition_isbn13 ON aib.ol_edition (isbn13);
CREATE INDEX IF NOT EXISTS ol_edition_isbn10 ON aib.ol_edition (isbn10);
