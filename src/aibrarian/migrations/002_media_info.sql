-- Full ffprobe metadata (all streams, HDR, subtitles, chapters) + chosen audio track.
ALTER TABLE aib.media_file
    ADD COLUMN IF NOT EXISTS media_info     JSONB,
    ADD COLUMN IF NOT EXISTS audio_layout   TEXT,      -- e.g. '5.1' of the selected/default track
    ADD COLUMN IF NOT EXISTS audio_stream_index INT,   -- ffprobe index used for transcription
    ADD COLUMN IF NOT EXISTS audio_language TEXT,
    ADD COLUMN IF NOT EXISTS probed_at      TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS media_file_audio_layout_idx ON aib.media_file (audio_layout);
-- allow non-transition bookkeeping events (re-probe)
ALTER TABLE aib.media_event DROP CONSTRAINT IF EXISTS media_event_outcome_check;
ALTER TABLE aib.media_event ADD CONSTRAINT media_event_outcome_check
    CHECK (outcome IN ('ok','failed','skipped','needs_review','reset','info'));
