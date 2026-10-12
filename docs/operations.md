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

## Automatic book librarian (`book-autoapply`)

`book-scan` (hourly :30) records book suggestions; `book-autoapply` (hourly :50 UTC,
concurrency 1 / CANCEL_NEW, cpu worker) applies them. No-op suggestions (target == source)
are hidden and never touched.

Per open book suggestion, oldest first, up to `BOOK_AUTO_BATCH_LIMIT` (default 50, 0 = all):

| Situation | Result |
|---|---|
| evidence score < `BOOK_AUTO_MIN_CONFIDENCE` (default **0.90**) | `needs_review`, file untouched |
| source missing | `skip` |
| ebook, sha256 equals an existing Calibre format | source **quarantined** (manifest, 30-day purge) |
| ebook, same title+author (or ISBN) already in Calibre, format missing | `add_format` (`calibredb add_format --dont-replace`) |
| ebook, same book and format already present but different bytes | `needs_review` |
| ebook, new | `calibre_add`: `calibredb add --library-path /books --automerge=ignore --title --authors --series --series-index --isbn --tags`, then source quarantined |
| calibredb fails / prints no book id | `error` + suggestion `needs_review`, **source untouched** |
| audiobook | moved to `/audiobookshelf/Author/[Series/][idx - ]Title/`; across mounts: copy → fsync → sha256 verify → source quarantined (`copy`) |
| target exists | identical → quarantine, different → `needs_review` (never overwrite) |

calibredb runs under an fcntl lock (`/data/reports/.calibre.lock`) and retries with backoff
on "database is locked" (Calibre-Web holds its own locks). Full stdout/stderr/exit code and
the new book id go to `details` in `aib.library_action` and the JSONL log, expandable on
`/library-log/ui`. The Calibre DB is never written directly. The image ships calibre
`CALIBRE_VERSION` (build arg, 9.16.0) — keep it ≥ the Calibre-Web copy
(`docker exec calibre-web calibredb --version`); container runs as uid 1000 like Calibre-Web.

### Evidence score

| Evidence | Score |
|---|---|
| ISBN lookup hit, title agrees | 0.97 |
| ISBN hit, title differs | 0.85 |
| embedded title + author, reference match | 0.50 + 0.45 × min(title%, author%) |
| embedded title + author, no reference (no index match) | 0.70 |
| embedded title only, reference | 0.40 + 0.30 × title% |
| filename / LLM guess only | 0.35 (+0.20 × title%) |
| junk title (hash, `unknown`, digits) | 0.10 |
| no author | capped at 0.30 |

Old suggestions are re-scored at apply time; no rescan needed.

### Title cleaning

`Dark Fortune: A Cozy Mystery (Fortunes, Love Book 6)` → title *Dark Fortune*, series
*Fortunes, Love* #6, subtitle *A Cozy Mystery*. Series/book markers (`Book 1`, `#3`,
`(Series 2)`) are split out, genre subtitles move to the subtitle, `" : "` is normalised;
non-genre subtitles stay (`Star Wars: Thrawn`).

```bash
dc exec aibrarian-api aibrarian books autoapply --limit 10            # dry run: confidence, PASS/below, decision
dc exec aibrarian-api aibrarian books autoapply --limit 10 --execute
dc exec aibrarian-api aibrarian books undo --id 42                    # dry run
dc exec aibrarian-api aibrarian books undo --run books-20261012T095000Z --execute
```

The dry run prints one line per suggestion on stderr (score, PASS/below, cleaned title/author/series, decision and the planned calibredb command)
and a table on stdout (`id, confidence, passes, decision, title, author, source, target, reason`).
`--json` gives the full plan.

`.env` knobs: `BOOK_AUTO_APPLY_ENABLED=true` (set `false` + `dc up -d aibrarian-worker` to
pause), `BOOK_AUTO_BATCH_LIMIT=50`, `BOOK_AUTO_MIN_CONFIDENCE=0.90`.

Book duplicates: the duplicates report and `dedup-quarantine` also cover identical books
across Calibre, Audiobookshelf, `book_drop` and `downloads` (book group numbers start at
10001). The keeper is the copy inside the library; only copies under `/media` are moved.

### Undo

`aibrarian books undo --id N` (repeatable) or `--run RUN` reverses `move` / `rename` /
`quarantine` actions newest first (dry run unless `--execute`): files and sidecars go back,
removed folders are recreated with their leftovers, the suggestion becomes `undone` (so it is
not re-applied), and an `undo` row is logged. It never overwrites; a blocked item is reported.

### Library log

Every `move`, `rename`, `quarantine`, `skip`, `needs_review`, `error`, `undo`,
`folder_removed` goes to `aib.library_action` (ts, run_id, domain, old/new path, title, author,
series, series index, confidence, reason, source `cli`/`prefect:<flow run id>`, details for
undo) and to `/data/reports/library-actions.log` (JSONL, outside the quarantine, never purged).

- Page: `http://z8g4:8790/library-log/ui` (today + total cards; filters for day/date range,
  title, author, series with autocomplete, action, domain, run; newest first; paging;
  read-only, "show command" prints the undo CLI). Linked from `/dashboard` and `/dedup/ui`.
- JSON: `GET /library-log?action=&domain=&since=&until=&title=&author=&series=&run_id=&limit=&offset=`,
  `GET /library-log/facets?field=author&q=her`.

### Calibre validation (`calibre-validate`)

Daily 05:41 UTC (concurrency 1) or `aibrarian books validate` (exit 1 on issues). Read-only:
sqlite `PRAGMA integrity_check` on a `mode=ro` connection, every logged ingest (book id still
present, file present, sha256 unchanged), formats missing on disk, orphan files in the
library (Calibre's own files are ignored: `metadata.db*`, `*.bak*`,
`metadata_db_prefs_backup.json`, `*.calnotes`, hidden dirs). `calibredb check_library` is *not* used because it VACUUMs the DB. The result is a
`validate` action, shown on `/library-log/ui`, `/dashboard` and `GET /library-log/validation`.

Undo: `calibre_add` → source restored, then `calibredb remove`; `add_format` →
`remove_format`; `copy` → source restored, the copy moved to `<root>/.aibrarian-trash/<date>/`.

### Adopting orphan files (`books adopt-orphans`)

`aibrarian books adopt-orphans [--limit N] [--execute] [--json]` (run in `aibrarian-worker`,
which has `calibredb`). Takes the validate orphan list, groups it by `<Author>/<Title>/`
folder and matches Calibre by normalized title + author (read-only sqlite):
existing book → `add_format` for missing formats, already-present formats are `duplicate`
(quarantined); no book → `calibre_add` (title/authors from the folder names, EPUB preferred)
then `add_format`. Only after calibredb succeeds are the loose files (plus leftover
cover/opf) moved to `/books/.aibrarian-quarantine/<date>/` (manifest.jsonl/tsv; same mount,
hidden from Calibre). Run id `adopt-<UTC>`; all rows on `/library-log/ui`;
`aibrarian books undo --run adopt-…` reverses it. Dry run by default. Prefect deployment
`calibre-adopt-orphans` exists but is manual only (no schedule, `execute=false`).
`quarantine purge` does not clean `/books/.aibrarian-quarantine`; remove old dates by hand.

## Open Library reference index (`openlibrary-index`)

The book scorer looks up a local index built from Tom's Open Library Delta Lake
(`/opt/openlibrary/delta` → `/openlibrary/delta:ro` in api + worker). Tables used: `authors`,
`works`, `editions` (`records` ignored). Only `record_key, revision, title, subtitle, name,
personal_name, isbn10, isbn13, works, authors` are read, one parquet file at a time in 50k-row
batches; `payload_json` is never read. Column types are inspected at runtime (logged as
`schema …`): `array<string>`, `array<struct{key}>`, `array<struct{author{key}}>` and JSON strings
all work. Several revisions of one `record_key`: the highest `revision` wins.

Output: `/data/olindex/openlibrary.sqlite` (`OL_INDEX_PATH`). SQLite rather than Postgres:
single-writer bulk load without WAL/bloat, several times faster, swapped in atomically
(`os.replace`) at the end so lookups keep using the old index during a rebuild; lookups are
read-only B-tree reads.

| table | content |
|---|---|
| `isbn` | ISBN-13 (ISBN-10 converted) → edition key |
| `edition` | key, title, subtitle, work key, author keys |
| `work` | key, title, subtitle, author keys |
| `author` | key → name |
| `tkey` | `surname|normalized title` → work (or edition without work) |
| `meta`, `done` | build state, row counts, `built_at`, finished files (resume) |

The build writes `openlibrary.sqlite.building`; a killed build resumes (finished parquet files
are skipped) as long as the Delta versions are unchanged. The weekly run (Sat 01:15 UTC,
concurrency 1) is a no-op when the Delta versions match the built index; `--rebuild` /
`rebuild=true` forces it.

Estimate: ~100k rows/s on a synthetic benchmark → about 1–3 h for the real tables
(editions dominate); disk ~20–30 GB for the finished file plus temporary sort files in the
same folder while the derived tables are built.

```bash
dc exec -d aibrarian-worker aibrarian openlibrary build-index   # or Prefect UI: openlibrary-index → Run
dc exec aibrarian-api aibrarian openlibrary stats                # progress / row counts / built_at
dc exec aibrarian-api aibrarian openlibrary lookup --isbn 1503071278
dc exec aibrarian-api aibrarian openlibrary lookup --title "Dune" --author "Frank Herbert"
curl -s z8g4:8790/openlibrary/stats
```

How the scorer uses it (also for old suggestions, at `books autoapply` time):

* ISBNs from the embedded metadata, a sidecar `Book.opf` / folder `metadata.opf`, and
  check-digit-valid digits in the file name (`1503071278.epub`) → ISBN match → 0.97
  (title agrees) or 0.85.
* Otherwise the author surname + main title (range read on `tkey`, fuzzy on the title) →
  `0.50 + 0.45 × min(title%, author%)`, max 0.95.
* On a strong match the Open Library author spelling is used, a junk title is replaced by
  the Open Library title, and `calibredb add` gets `--identifier openlibrary:OL…M` (or `…W`)
  and the ISBN. Series are not in these columns, so series still come from the epub /
  title cleaning.

## Embedding model A/B (`embed-eval`)

Compares a candidate embedding model (e.g. `embeddinggemma-2`) with the production
`EMBED_MODEL` (`nomic-embed-text`) without touching production: everything goes to separate
Qdrant collections `eval_<model>_segments` / `eval_<model>_windows`, state and reports to
`/data/reports/embed-eval/`. Postgres is only read.

Task prefixes come from a small registry (`aibrarian.ollama.EMBED_PREFIXES`, default for
unknown models = nomic, i.e. unchanged behaviour):

| model | query prefix | document prefix | source |
|---|---|---|---|
| nomic-embed-text | `search_query: ` | `search_document: ` | nomic model card |
| embeddinggemma / embeddinggemma-2 | `task: search result \| query: ` | `title: none \| text: ` | Google model card (HF `google/embeddinggemma-2`, prompts `Retrieval-query` / `Retrieval-document`) |

Documents use `title: none` on purpose (the movie title would leak the answer into the eval).

* `build --model M --movies 20`: picks 20 movies of `01-movies` with ≥ 40 segments, evenly
  spaced by duration (deterministic; stored in `state.json`), embeds segments + windows with
  the baseline and `M` in batches of 16, 0.2 s pause, `keep_alive=2m`, `nice 10`. Resumable per
  (model, movie); `--reselect` picks again.
* `run`: query set (cached in `queries.json`, `--regen` to rebuild): 20 exact quotes, 20
  paraphrases and 20 "scene where …" descriptions written by `qwen3:14b`, each with ground
  truth (movie + time). `--queries file.jsonl` adds `{"query", "movie_id", "start", "end"}`
  rows (without ground truth they are only shown). Hit = right movie and within ±30 s
  (`--tolerance`). Metrics per model × index (segments / windows) × query type: R@1/5/10, MRR,
  plus median query latency (embed + search), embed throughput (texts/s during build) and
  VRAM from Ollama `/api/ps`. Writes `latest.md/json` and `report-<ts>.md/json`.
* `/embed-eval/ui`: latest report + one query box showing the top 5 of every built model
  side by side (read-only; it embeds the query with each model).
* `cleanup`: drops every `eval_*` collection (reports and the movie pick are kept).

GPU note: `OLLAMA_MAX_LOADED_MODELS=2` keeps `qwen3:14b` + `nomic-embed-text` resident. Loading
the candidate evicts the least recently used one for a few minutes; the next production call
reloads it (seconds, no failures). Run the build when the pipeline is quiet if possible.
CPU/disk: the build is GPU-bound and light on disk (Qdrant upserts of ~a few 10k points), so it
can run alongside `openlibrary-index`.

```bash
dc exec ollama ollama pull embeddinggemma-2
dc exec aibrarian-api aibrarian embed-eval build --model embeddinggemma-2 --movies 20
dc exec aibrarian-api aibrarian embed-eval run
dc exec aibrarian-api aibrarian embed-eval query "scene where they argue about the money"
dc exec aibrarian-api aibrarian embed-eval cleanup
```
