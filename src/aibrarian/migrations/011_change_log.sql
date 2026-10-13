-- Central change log: every filesystem move/rename/quarantine/restore and every
-- path-bearing DB row change, written 'pending' BEFORE the filesystem op and set to
-- committed/failed/rolled_back afterwards (see aibrarian/changelog.py).
CREATE TABLE IF NOT EXISTS aib.change_log (
    id           BIGSERIAL PRIMARY KEY,
    ts           TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at  TIMESTAMPTZ,
    actor        TEXT,                 -- 'cli', 'api', 'prefect:<flow name>' ...
    run_id       TEXT,                 -- Prefect flow run id (or NULL)
    txn_id       TEXT NOT NULL,        -- correlation id: all rows of one logical change
    op           TEXT NOT NULL CHECK (op IN ('move','rename','quarantine','restore','delete_row',
                                             'insert','update_path','relink')),
    entity       TEXT NOT NULL,        -- media_file | transcript | book | suggestion | sidecar
    entity_id    BIGINT,
    old_path     TEXT,
    new_path     TEXT,
    sha256       TEXT,
    size_bytes   BIGINT,
    status       TEXT NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending','committed','failed','rolled_back')),
    error        TEXT,
    details      JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS change_log_ts_idx ON aib.change_log (ts DESC);
CREATE INDEX IF NOT EXISTS change_log_pending_idx ON aib.change_log (ts) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS change_log_old_path_idx ON aib.change_log (old_path);
CREATE INDEX IF NOT EXISTS change_log_new_path_idx ON aib.change_log (new_path);
CREATE INDEX IF NOT EXISTS change_log_txn_idx ON aib.change_log (txn_id);
