-- Durable log of every dedup quarantine/restore/purge/skip/error (CLI and Prefect).
CREATE TABLE IF NOT EXISTS aib.dedup_action (
    id               BIGSERIAL PRIMARY KEY,
    ts               TIMESTAMPTZ NOT NULL DEFAULT now(),
    action           TEXT NOT NULL CHECK (action IN ('quarantine','skip','restore','purge','error',
                                         'folder_removed')),
    group_no         INT,
    media_id         BIGINT,
    keeper_path      TEXT,
    file_path        TEXT NOT NULL,
    quarantine_path  TEXT,
    size_bytes       BIGINT,
    sha256           TEXT,
    sidecars_copied  JSONB NOT NULL DEFAULT '[]'::jsonb,
    reason           TEXT,
    source           TEXT          -- 'cli', 'api', 'backfill' or 'prefect:<flow run id>'
);
CREATE UNIQUE INDEX IF NOT EXISTS dedup_action_uniq ON aib.dedup_action (action, file_path, ts);
CREATE INDEX IF NOT EXISTS dedup_action_ts_idx ON aib.dedup_action (ts DESC);
