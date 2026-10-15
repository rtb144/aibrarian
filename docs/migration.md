# Migrating from the old `/opt/stacks/ai` stack

Inputs used for this redesign: the compose summary, the 20 generated tool
summaries in `/opt/stacks/docs`, the 2026-10-09 `gen_docs.log`, and the
sources of `librarian.py`, `catalog_api.py`, `catalog_db.py` and
`vector_store.py`. The original scripts are **not** in this repo.

## Old tool → new home

| Old tool (service / port) | New module | Prefect task / flow | CLI / API |
|---|---|---|---|
| `transcriber.py` (`movie-transcriber`, GPU0) | `actions` (`discover`, `probe`, `extract_audio`, `transcribe`, `embed`), `transcribe`, `sidecars`, `probe` | tasks `action:probe` … `action:embed`; flow **`media-pipeline`** (every `AIB_TRANSCRIBE_INTERVAL_S`=300 s, batch `AIB_TRANSCRIBE_BATCH_SIZE`=5) | `aibrarian pipeline`, `aibrarian run-action transcribe --id N` |
| `catalog_db.py` | `db` (+ `migrations/001_init.sql`) | used by every task | `aibrarian migrate` |
| `vector_store.py` | `vectors` (Qdrant REST; same `movie_segments` collection, cosine 768, payload `movie_id`, point id = segment id, `count`, `min_score`) | `action:embed` | |
| `catalog_api.py` (`catalog-api` :8790) | `api`, `search` | | same routes on **:8790**: `/movies`, `/movies/lookup`, `/movies/by-path`, `/movies/{id}`, `/movies/{id}/location`, `/movies/{id}/segments`, `/search/text`, `/search/semantic`; new `/search` (hybrid) |
| `movie_clipper.py` (`movie-clipper` :8791) | `clips`, `services.make_clip` | | `POST /clip` (same body: `phrase`, `movie_id`, `title`, `rel_path`, `match_index`), host port 8791 still maps to the API; `aibrarian clip` |
| `movie_windows.py` (index/search, `movie_windows` collection) | `windows` | part of `action:embed` (windows built automatically, `--size 10 --stride 5` → `AIB_WINDOW_SIZE/STRIDE`) | `GET /search/windows`, `aibrarian search --windows` |
| `movie_eta.py` | `eta` | | `GET /status` (`eta`), `aibrarian status`, dashboard header |
| `librarian.py` (`ai-jellyfin-librarian` :8787) | `video_librarian`, `jellyfin`, `services.library_scan` | per-video: `action:catalog`; whole library incl. `03-music` + `downloads`: flow **`library-scan`** (daily 03:00) | `POST /scan?max_items=&resume=`, `GET /report/latest` (host port 8787 still maps to the API); `aibrarian library-scan` |
| `ingest_report_to_graph.py` (Neo4j) | replaced by `aib.scan_run` + `aib.suggestion` (no graph DB) | written by `library-scan` | `GET /report/latest`, `GET /suggestions` |
| `apply_moves.py` | `fileops.move_with_sidecars`, `services.apply_suggestion` | `book-scan` (auto mode only) | `aibrarian apply [--domain --kind --op move --min-confidence] [--apply]`, `POST /suggestions/{id}/apply?dry_run=false` |
| `apply_renames.py` | same (an `op=rename` suggestion is one whose target is in the same directory) | | `aibrarian apply --op rename --kind music --min-confidence 0.9 --apply` |
| `delete_skip_dst_exists.py` | `fileops.delete_skipped_duplicates` (now also requires **equal size** before deleting) | | `aibrarian cleanup-skipped [--log old_move_log.jsonl] [--path-map /media=/opt/jellyfin/media] [--apply]` |
| `sync_srt_to_media_from_logs.py` | `fileops.sync_orphan_subtitles`. New moves carry their sidecars, so this is only needed for history | | `aibrarian sync-subtitles --log move_log_X.jsonl --log rename_log_Y.jsonl [--apply]` |
| `compare_rename_move_srt.py` | `fileops.subtitle_report` | | `aibrarian srt-report [--log …]` |
| `catalog_srt_files.py` | `sidecars.catalog_srt_files` | | `aibrarian srt-catalog --format csv --unpaired-only --output x.csv` |
| `book_librarian.py` (`ai-book-librarian` :8788) | `books`, `services.book_scan` | flow **`book-scan`** (hourly at :30) | `POST /book-scan`, `GET /book-suggestions` (host port 8788 still maps to the API); `aibrarian book-scan` |
| `import_openlibrary_pg.py` (`openlibrary-import-pg`) | `openlibrary.import_dump` (COPY via a staging table, NUL stripping, batches, `--truncate`, idempotent upsert) | flow **`openlibrary-import`** (manual) | `aibrarian openlibrary-import /data/openlibrary/<dump> [--truncate]` |
| `openlibrary_catalog.py` (DuckDB, :8090) | `openlibrary.lookup` (Postgres pg_trgm, ISBN exact match → confidence 1.0) | used by `book-scan` | `GET /openlibrary/lookup?title=&author=&isbn=`, `GET /openlibrary/stats` |
| `gen_docs.py` | `gendocs` (fixes from the log, see below) | | `aibrarian gen-docs src --out docs/reference` |
| Neo4j, Redis | **removed** (see [architecture](architecture.md#key-design-decisions)) | | |
| Prefect server + services + worker | `prefect-server` (single process, no Redis) + two `serve` workers | | UI :4200 |

## Environment variables

Old names still work, because `config.py` accepts them as aliases. If both are
set, the `AIB_*` name wins.

| Old | New |
|---|---|
| `CATALOG_DSN` (or `CATALOG_DB_*` + `PREFECT_DB_PASSWORD`) | `AIB_DATABASE_URL` (`CATALOG_DSN` accepted) |
| `QDRANT_URL`, `QDRANT_COLLECTION`, `EMBED_DIM`, `WINDOW_COLLECTION` | `AIB_QDRANT_URL`, `AIB_QDRANT_COLLECTION`, `AIB_EMBED_DIM`, `AIB_WINDOW_COLLECTION` |
| `OLLAMA_URL`, `OLLAMA_MODEL`, `EMBED_MODEL` | `AIB_OLLAMA_URL`, `AIB_LLM_MODEL`, `AIB_EMBED_MODEL` |
| `WHISPER_MODEL`, `SCAN_INTERVAL_S`, `BATCH_SIZE` | `AIB_WHISPER_MODEL`, `AIB_TRANSCRIBE_INTERVAL_S`, `AIB_TRANSCRIBE_BATCH_SIZE` |
| `MEDIA_ROOT`, `MEDIA_ROOTS` | `AIB_MEDIA_ROOT`, `AIB_TRANSCRIBE_ROOTS` (default now `01-movies,02-tvshows,05-instructional_videos`; the old default was `01-movies`) |
| `RETRY_FAILED` | gone: failures auto-retry 3× then wait for `retry-failed` |
| `JELLYFIN_URL`, `JELLYFIN_API_KEY` | `AIB_JELLYFIN_URL`, `AIB_JELLYFIN_API_KEY` |
| `REPORT_DIR`, `BOOK_REPORT_DIR` | `AIB_REPORT_DIR` (reports now live in Postgres; this dir only holds `file_ops.jsonl`) |
| `RESUME_DEFAULT`, `progress_*.jsonl`, `book-progress.jsonl` | `resume=true` (default) on `/scan`, `/book-scan`; progress = rows in `aib.suggestion` |
| `BOOK_SOURCE_DIRS`, `BOOK_MIN_CONFIDENCE`, `BOOK_REVIEW_THRESHOLD` | `AIB_BOOK_SOURCE_DIRS`, `AIB_BOOK_MIN_CONFIDENCE`, `AIB_BOOK_REVIEW_CONFIDENCE` |
| `OUT_DIR`, `CLIP_PAD_SECONDS`, `FUZZY_MIN_SCORE` | `AIB_CLIPS_DIR`, `AIB_CLIP_PADDING_S`, `AIB_CLIP_FUZZY_THRESHOLD` |
| `GRAPHDB_*`, `GRAPH_SCHEMA_PATH`, `GRAPH_CANONICAL_ROOTS` | gone (canonical roots: `AIB_CANONICAL_ROOTS` JSON, default movie→01-movies, tv→02-tvshows, music→03-music) |
| `OPENLIBRARY_DSN`, `OPENLIBRARY_DB` | gone: same database, table `aib.ol_edition` |

## Data migration

| Data | Plan |
|---|---|
| Transcripts | **Kept on disk.** Old `.transcript.json` sidecars are reused when newer than the video (`AIB_REUSE_LEGACY_SIDECARS=true`), so nothing is re-transcribed. On reuse they are rewritten in the new format (with fingerprint + model). The pipeline still runs probe → embed → catalog for each file. That costs GPU1 time, not GPU0. |
| `movie_catalog` Postgres DB | Not copied: everything in it is rebuilt from disk and sidecars. Keep the old `prefect-postgres` volume until you're happy. |
| Qdrant `movie_segments` / `movie_windows` | Point ids were old `segments.id`s, which no longer match. Start the new Qdrant on a **new volume** (the default `qdrant_data`); the pipeline re-embeds. Drop the old volume afterwards. |
| Neo4j graph, `report_*.json`, `progress_*.jsonl` | Not migrated (it was derived data). Run `library-scan` once to regenerate suggestions. Keep the old reports dir as an archive. |
| Old `move_log_*.jsonl` / `rename_log_*.jsonl` | Still usable: `cleanup-skipped`, `sync-subtitles` and `srt-report` accept `--log` and normalise the old key names. |
| Open Library | Re-import the dump: `aibrarian openlibrary-import /data/openlibrary/<file>` (about 1–2 h for editions). Drop the old `openlibrary` DB and `openlibrary.duckdb` afterwards. |
| Ollama models | Point the new `ollama_models` volume at the old one (see the ROADMAP step), or just `ollama pull` again. |
| Whisper model cache | New `whisper_models` volume. First start downloads large-v3-turbo (~1.6 GB). |

Step-by-step instructions are in [ROADMAP.md](ROADMAP.md).

## Problems in the old setup and how they're addressed

| Problem (source) | Fix |
|---|---|
| `pip install` at every container start; scripts bind-mounted (compose summary) | wheels baked into two images; no bind-mounted code |
| Subtitles orphaned after renames/moves, so three scripts existed to detect and repair it (summaries) | `fileops.move_with_sidecars` moves `.srt/.ass/.vtt/.nfo/.transcript.json` together with the video |
| `delete_skip_dst_exists` deleted based on existence only | also requires equal size, dry-run by default, audited |
| Reports, progress and graph in three places (JSON, JSONL, Neo4j) | one table set (`scan_run`, `suggestion`, `file_op`) |
| No per-item visibility; ETA was a separate script | state machine + events + `/status`, `/dashboard`, metrics |
| `gen_docs.log`: it walked `/opt/stacks`, found **565** Python files, and spent hours documenting `venv/lib/python3.14/site-packages/neo4j/*` (Neo4j driver internals) | `gendocs` skips `venv`, `.venv`, `site-packages`, `node_modules`, `.git` and caches, and scans only the paths you pass (default `src`) |
| `gen_docs.log`: compose discovery took 8–18 min (19:40→19:48, 21:22→21:40) | discovery is a pruned `os.walk` with the same excludes |
| `gen_docs.log`: two runs stopped mid-way with no closing line (20:38 run ends at 21:09 inside `neo4j/_async/work/result.py`; 21:22 run) | `generate()` always logs a final `done ok= failed= skipped=` line (in `finally`) and logs per-file errors instead of dying |
| `gen_docs.log`: everything regenerated on each run | content-hash cache (`.gendocs-cache.json`) skips unchanged files; `--force` overrides |
| Host Python 3.14 venv inside `/opt/stacks/ai` (log paths) | everything runs in containers (Python 3.12); delete that venv after cut-over |
| `openlibrary-catalog` (DuckDB, :8090) duplicated the Postgres `openlibrary` import | one Postgres table, served by the API |

## Behaviour changes to be aware of

- `05-instructional_videos` and `02-tvshows` are now transcribed too, which grows the backlog. Remove them from `AIB_TRANSCRIBE_ROOTS` to keep the old scope.
- Port **8790** is the API for everything. 8787, 8788 and 8791 are compatibility aliases on the same API, so their **paths** must be the new ones. For example, `POST :8791/clip` still works, but `:8787/health` now returns the unified health.
- `/report/latest` now returns the latest `scan_run` with its suggestion rows instead of the old JSON report shape (`items[].parsed` → `items[].metadata.parsed`).
- Old `/movies?status=` values (`pending|transcribing|transcribed|embedded|failed`) are now stages (`discovered|…|cataloged|failed|…`).
- Neo4j Browser (:7474/:7687) is gone. Memgraph in the devtools profile uses 127.0.0.1:7687 only for the *code* graph.
