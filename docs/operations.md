# Operations runbook

All commands run from the repo checkout on z8g4 (e.g. `/opt/stacks/aibrarian`).
`dc` below is short for `docker compose`.

## Daily glance

- Dashboard: `http://z8g4:8790/dashboard` (stage cards, failures, ETA)
- Prefect: `http://z8g4:4200` (flow runs, schedules, retries)
- Grafana: the "aibrarian" dashboard (import `docker/grafana/aibrarian-dashboard.json`)
- From the CLI: `dc exec aibrarian-api aibrarian status`

## Services

| Service | Port | Health |
|---|---|---|
| aibrarian-api | 8790 (+ 8787/8788/8791 aliases) | `curl -fsS z8g4:8790/health` (db + qdrant/ollama/jellyfin flags) |
| aibrarian-gpu | metrics 9109 | `dc logs -f aibrarian-gpu`, `nvidia-smi` shows whisper on GPU0 |
| aibrarian-worker | metrics 9108 | `dc logs -f aibrarian-worker` |
| prefect-server | 4200 | `curl z8g4:4200/api/health` |
| postgres | internal | `dc exec postgres pg_isready` |
| qdrant | 6333/6334 | `curl z8g4:6333/collections` |
| ollama | 11434 | `curl z8g4:11434/api/tags` |

## Common tasks

| I want to… | Do |
|---|---|
| see why an item failed | dashboard → *failed* card → click the row; or `aibrarian timeline <id>` |
| retry all failures | `dc exec aibrarian-api aibrarian retry-failed` (or run the `retry-failed` deployment in Prefect) |
| retry one item | `curl -X POST z8g4:8790/media/<id>/retry` |
| run one step by hand | `curl -X POST z8g4:8790/media/<id>/actions/probe` |
| kick the pipeline now | Prefect UI → `media-pipeline` → Run, or `dc exec aibrarian-gpu aibrarian pipeline` |
| rescan the disks now | Prefect UI → `discover` → Run, or `dc exec aibrarian-worker aibrarian discover` |
| pause transcription | Prefect UI → Deployments → `media-pipeline` → pause schedule |
| reprocess one file | `touch` it (the fingerprint changes and it goes back to `discovered`), or `UPDATE aib.media_file SET stage='audio_extracted' WHERE id=…` to redo from transcribe |
| review placement suggestions | `GET /suggestions?domain=video` → `POST /suggestions/<id>/apply` (dry run) → `?dry_run=false` |
| apply confident movie moves | `aibrarian apply --domain video --kind movie` (check the plan) → add `--apply` |
| dismiss a suggestion | `POST /suggestions/<id>/dismiss` |
| scan books now | `curl -X POST z8g4:8790/book-scan -H 'content-type: application/json' -d '{}'` |
| find a quote | `curl 'z8g4:8790/search?q=i%27ll+be+back'` |
| cut a clip | `curl -X POST z8g4:8790/clip -H 'content-type: application/json' -d '{"phrase":"I will be back","title":"Terminator"}'` → `/opt/stacks/aibrarian/data/clips/` |
| fix orphaned subtitles from old logs | `aibrarian srt-report --log …` then `aibrarian sync-subtitles --log … --apply` |
| regenerate code docs | `aibrarian gen-docs src --out docs/reference` |

## Upgrades

```bash
git pull
dc build aibrarian-api aibrarian-gpu
dc up -d          # migrations apply automatically on API/worker start (advisory-locked)
```

To add a schema change, create `src/aibrarian/migrations/00N_name.sql`. Files
apply in name order, each once, and are recorded in `aib.schema_migrations`.

## Backups

- Postgres: `dc exec postgres pg_dump -U aibrarian -Fc aibrarian > aib_$(date +%F).dump`. Prefect's DB is optional.
- Qdrant: rebuildable (re-embed), but snapshots are faster: `curl -X POST z8g4:6333/collections/movie_segments/snapshots`.
- Sidecars live next to the media and are covered by your media backup.

## Troubleshooting

| Symptom | Check |
|---|---|
| Many `transcribe` failures with CUDA errors | `nvidia-smi` on GPU0; `dc logs aibrarian-gpu`; host driver must support CUDA 12.8 (driver ≥ 570) |
| Two whisper jobs at once or GPU OOM | run `aibrarian prefect-limits` (creates the `gpu-whisper=1` tag limit) |
| `embed`/`catalog` slow or timing out | `ollama ps` (are both models loaded?), GPU1 memory, `AIB_LLM_TIMEOUT_S` |
| Items stuck in a stage with no events | stale lease: they're reclaimed after 6 h, or `UPDATE aib.media_file SET locked_at=NULL WHERE …` |
| `needs_review` piling up | Jellyfin API key wrong (`/health` → `jellyfin:false`), or titles are unusual: review in `/suggestions` |
| Moves report `skip_dst_exists` | the target already exists; compare the files, then `aibrarian cleanup-skipped` (dry run first) |
| Sidecar write warnings | the media mount is read-only or owned by someone else; the GPU worker runs as root and chowns to match the video |
| Logs | `dc logs <svc> \| jq 'select(.level=="error")'` (JSON lines) |

## Quote search by movie title

- UI: `http://<host>:8790/search/ui` (title box autocompletes from transcribed movies only).
- `GET /search?q=...&title=...` (also `/search/text`, `/search/semantic`): title is
  fuzzy-resolved (case/punctuation-insensitive, typos, optional `(year)`) against items
  that have transcript segments. Response stays a JSON list; the match is in headers
  `X-Resolved-Title` (URL-quoted), `X-Media-Id`, `X-Title-Score`. No match → 404,
  ambiguous (e.g. two "Alfie" films) → 422; both include `detail.suggestions`.
- `GET /titles/autocomplete?q=jump&limit=10`, `GET /titles/resolve?title=...`.
- `POST /clip` with `title` uses the same resolver.
- CLI: `aibrarian search "quote" --title "22 Jump Street (2014)"`.

## Reviewing and applying suggestions

- No-op suggestions (target == source, or op `none`) are stored with status `noop`
  and hidden from `GET /suggestions` / `aibrarian suggestions` unless
  `include_noop=true` / `--include-noop`.
- List: `aibrarian suggestions --domain video --tsv` (id, confidence, decision, op, source, target).
- Dry-run specific ids: `aibrarian apply --id 12 --id 15` (filters are ignored when `--id` is given).
- Execute: `aibrarian apply --id 12 --id 15 --apply`. Sidecars move with the file, an
  existing destination is refused (suggestion stays open), the catalog path is
  updated (segments stay linked by id), an `apply_suggestion` event is recorded and
  Jellyfin is asked to rescan.
- API: `POST /suggestions/{id}/apply` (dry-run unless `dry_run=false`),
  `POST /suggestions/{id}/reject`. CLI: `aibrarian reject --id N`.

## Duplicates report (read-only)

`aibrarian duplicates [--exact-only] [--tsv]`, `GET /duplicates` (`?cached=true` for the
last saved report, `?exact_only=true`), page `/duplicates/ui`, Prefect deployment
`duplicates` (Sundays 04:00). The report itself never deletes or moves anything (see
"Quarantining identical duplicates" below for the opt-in move).

- **identical**: same size → same head+tail sample → same full sha256. The catalog
  fingerprint mixes in mtime so it can't be used; content hashes are cached in
  `aib.file_hash` (path+size+mtime), so re-runs only hash new/changed candidates.
- **review**: same normalized title+year, or duration within 2 s when files share a
  folder or have similar titles. Compare codec/resolution/size/audio/subs side by side.
- `suggested_keep` prefers `Title (Year)/Title (Year).ext`, then resolution, bitrate,
  stream counts, sidecars. `sidecars_only_on_others` lists subtitles etc. that exist only
  next to a non-kept copy, with the name they would get next to the kept file.

## Quarantining identical duplicates

Only **identical** (byte-for-byte) groups. Nothing is ever deleted: extra copies are
*moved* (instant `rename`, same filesystem) into a quarantine folder and can be restored.

```bash
dc exec aibrarian-api aibrarian duplicates quarantine --limit 3          # dry run (plan)
dc exec aibrarian-api aibrarian duplicates quarantine --group 4 --group 7 # only these groups
dc exec aibrarian-api aibrarian duplicates quarantine --limit 3 --execute # move them
dc exec aibrarian-api aibrarian duplicates restore --manifest /media/_quarantine/2026-10-12/manifest.jsonl            # dry run
dc exec aibrarian-api aibrarian duplicates restore --manifest /media/_quarantine/2026-10-12/manifest.jsonl --execute
```

- Group numbers are the `group` column of `aibrarian duplicates --exact-only --tsv` / the
  `#N` on `/duplicates/ui` (identical section). The plan is recomputed from a fresh
  exact-only report each run (hash cache makes this fast), so check it before `--execute`.
- The plan lists group, keeper, extra copy, size, destination and a GB total.
  `--json` prints the full plan/result.
- Per copy, at execute time: keeper and copy must exist, sizes must match and a **fresh**
  full sha256 of both must match (cached hashes are not trusted), else it's skipped.
  Sidecars (`.srt/.ass/.sub/.vtt/.nfo/.transcript.json`) that exist only next to the
  copy are **copied** next to the keeper under the keeper's name (never overwriting);
  the copy's own sidecars travel with it into quarantine.
- Destination: `$QUARANTINE_DIR/<YYYY-MM-DD>/<path relative to /media>`. Default
  `QUARANTINE_DIR=/media/_quarantine` (container path, `AIB_QUARANTINE_DIR` also works).
  It must be on the media filesystem; a cross-device move is refused.
- Each batch folder holds `manifest.jsonl` (used by `restore`) and `manifest.tsv`;
  restores are logged in `restored.jsonl` so a restore is idempotent and never overwrites.
- Catalog: the moved row is repointed to the quarantine path, `missing=TRUE`,
  `stage='skipped'`, with a `quarantine` event in `aib.media_event` (`aibrarian timeline <id>`).
  Restore puts back the original path and previous stage and logs a `restore` event.
- `discover`, `library-scan` and `srt-catalog` skip any `_quarantine` directory and
  `QUARANTINE_DIR`.
- `/duplicates/ui` shows a read-only quarantine preview (`GET /duplicates/quarantine-plan`);
  there is no button that moves files.
- Once you're happy (e.g. Jellyfin plays the keepers), delete the dated folder by hand.

### Progress output

`duplicates quarantine` prints one progress line per file on stderr (flushed):
`[k/n] group G: verifying sha256 (X GB x2) <path>`, then `moved -> …` / `SKIPPED (reason)`.
Building the exact-only report comes first and can take a few minutes on the first run.

### Empty source folders

After `--execute` (CLI and the `dedup-quarantine` flow) the original folder of each moved
copy is removed when nothing but leftovers remains: `.nfo .jpg .jpeg .png .txt .url`,
`Thumbs.db`, `.DS_Store` (subtitles `.srt/.ass/.sub/.idx` or any other file keep it).
Leftovers are **moved** into the same dated quarantine folder (never deleted outright), then
the parent folders are checked the same way. `/media` itself, the library roots
(`/media/01-movies` …, anything less than two levels deep), the quarantine dir and anything
outside `/media` are never removed. Each removal is a `folder_removed` row in the action log;
`restore` recreates the folders and puts the leftovers back.

One-off for batches moved before this existed:

```bash
dc exec aibrarian-api aibrarian duplicates cleanup-empty            # dry run
dc exec aibrarian-api aibrarian duplicates cleanup-empty --execute
```

## Automatic dedup and retention (Prefect)

| Deployment | Schedule (UTC) | What |
|---|---|---|
| `duplicates` | Sun 04:00 | read-only report |
| `dedup-quarantine` | Sun 05:00 | `duplicates quarantine --execute` for **identical groups only** (never review groups): fresh sha256 re-check, sidecars copied to keeper, empty folders removed |
| `quarantine-purge` | daily 04:37 | **permanently deletes** quarantined copies older than `QUARANTINE_RETENTION_DAYS` |

All run with concurrency 1 / CANCEL_NEW on the cpu worker (`aibrarian-worker`).

`.env` knobs (passed through docker-compose.yml):

- `DEDUP_AUTO_ENABLED=true`: set `false` to make `dedup-quarantine` a no-op.
- `DEDUP_BATCH_LIMIT=50`: extra copies per weekly run (`0` = unlimited).
- `QUARANTINE_RETENTION_DAYS=30`.

Purge rules: age comes from `moved_at` in the manifest (fallback: the dated folder name);
only files listed in a manifest (`manifest.jsonl` / `folders.jsonl`) are deleted, only if they
resolve inside `QUARANTINE_DIR` and aren't symlinks. Each purge adds a `purged` event in
`aib.media_event` and sets `aib.media_file.purged_at` (stage stays `skipped`). Emptied dated
folders are removed; their manifests are archived in `QUARANTINE_DIR/.manifests/<date>/`.

```bash
dc exec aibrarian-api aibrarian duplicates purge                       # dry run, uses retention
dc exec aibrarian-api aibrarian duplicates purge --older-than-days 0    # dry run: what is in there
dc exec aibrarian-api aibrarian duplicates purge --execute
dc exec aibrarian-api aibrarian duplicates quarantine-status            # contents, age, purge dates
```

## Dedup action log

Every quarantine / skip / error / restore / purge / folder_removed is recorded in
`aib.dedup_action` (time, group, keeper, file, quarantine path, size, sha256, sidecars,
reason, source `cli` or `prefect:<flow run id>`) **and** appended to
`QUARANTINE_DIR/dedup-actions.log` (JSONL; purge never touches it). Manifests written before
the table existed are back-filled automatically when the API or worker starts (idempotent).

- Page: `http://z8g4:8790/dedup/ui` (summary cards, filters by action/date/title, paging;
  read-only). Linked from `/dashboard` and `/duplicates/ui`.
- JSON: `GET /dedup/actions?action=&since=&until=&q=&limit=&offset=`.
- `/duplicates/ui` also shows what is in quarantine, its age and the next purge run
  (`GET /duplicates/quarantine`).
