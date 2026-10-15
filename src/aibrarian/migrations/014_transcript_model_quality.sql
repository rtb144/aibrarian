-- transcription model tracking + per-transcript quality signals (for a validation/re-run pass).
-- aib.transcript.model already holds the whisper model name; add compute type and
-- faster-whisper quality signals (all nullable). Everything transcribed before this
-- migration used large-v3 float16.
ALTER TABLE aib.transcript ADD COLUMN IF NOT EXISTS compute_type TEXT;
ALTER TABLE aib.transcript ADD COLUMN IF NOT EXISTS language_probability DOUBLE PRECISION;
ALTER TABLE aib.transcript ADD COLUMN IF NOT EXISTS avg_logprob_mean DOUBLE PRECISION;
ALTER TABLE aib.transcript ADD COLUMN IF NOT EXISTS max_compression_ratio DOUBLE PRECISION;
UPDATE aib.transcript SET model = 'large-v3' WHERE model IS NULL OR model = '';
UPDATE aib.transcript SET compute_type = 'float16' WHERE compute_type IS NULL AND model = 'large-v3';
CREATE INDEX IF NOT EXISTS transcript_model_idx ON aib.transcript (model);
