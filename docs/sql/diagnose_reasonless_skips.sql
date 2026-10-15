-- Read-only: infer why video rows are stage='skipped' with no last_error.
-- Uses the media_event history (details.reason was always logged there) and
-- the probe JSON (audio list like 'eng:aac:stereo', duration) / audio_path.
-- Run: psql "$DATABASE_URL" -f docs/sql/diagnose_reasonless_skips.sql
SELECT m.id,
       m.path,
       m.duration_s,
       jsonb_array_length(COALESCE(m.probe->'audio', '[]'::jsonb))      AS audio_streams,
       m.probe->'audio'                                                AS audio_tracks,
       m.audio_path IS NOT NULL                                        AS reached_extract,
       e.action                                                        AS skipped_by_action,
       e.from_stage,
       e.details->>'reason'                                            AS logged_reason,
       e.details->>'language'                                          AS whisper_language,
       e.finished_at                                                   AS skipped_at,
       CASE
         WHEN e.details->>'reason' IS NOT NULL
              THEN 'skip: ' || (e.details->>'reason') || ' (from media_event)'
         WHEN m.probe IS NULL
              THEN 'unknown: never probed'
         WHEN jsonb_array_length(COALESCE(m.probe->'audio', '[]'::jsonb)) = 0
              THEN 'inferred: no audio stream'
         WHEN m.audio_path IS NOT NULL
              THEN 'inferred: no speech detected (audio extracted, transcript empty)'
         ELSE 'inferred: has audio, not extracted -> check media_event'
       END                                                             AS inferred_reason
FROM aib.media_file m
LEFT JOIN LATERAL (
    SELECT ev.action, ev.from_stage, ev.details, ev.finished_at
    FROM aib.media_event ev
    WHERE ev.media_id = m.id AND ev.to_stage = 'skipped'
    ORDER BY ev.id DESC LIMIT 1
) e ON TRUE
WHERE m.kind = 'video' AND m.stage = 'skipped' AND m.last_error IS NULL
ORDER BY inferred_reason, m.id;
