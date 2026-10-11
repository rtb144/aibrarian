# aibrarian

Self-hosted AI media librarian and video transcription stack for **z8g4**
(2× NVIDIA GPUs, Docker Compose). It replaces the collection of scripts in
`/opt/stacks/ai` with **one Python 3.12 package**, **one Postgres schema**, and **one
HTTP API**. Every step is tracked per media item.

What it does:

- **Transcribes** every video under `01-movies`, `02-tvshows` and
  `05-instructional_videos` with faster-whisper `large-v3` on **GPU0**. It
  writes `.srt` and `.transcript.json` sidecars next to each video, and Jellyfin
  picks the `.srt` files up.
- **Indexes** transcripts for search. Embeddings come from Ollama
  `nomic-embed-text` on **GPU1** and go into Qdrant (`movie_segments` holds lines,
  `movie_windows` holds scenes). Keyword search uses Postgres trigram and full-text indexes.
- **Identifies** files with `qwen3:14b`, matches them against **Jellyfin**
  (`host:8096`) and *suggests* the canonical place and name for each one
  (`01-movies/Title (Year)/…`, `02-tvshows/Series/Season 01/…`, `03-music/Artist/Album/…`).
- **Books and audiobooks**: reads metadata from EPUB, PDF and audio tags. It
  matches against a local Open Library copy, then Google Books, then the LLM,
  and suggests Calibre or Audiobookshelf destinations. Confidence thresholds:
  0.85 means suggest (or auto-apply in auto mode), 0.60 means review.
- **Clips**: give it a quote and it returns a padded mp4. Matching uses
  word-level timestamps, with segment-level matching as a fallback.
- **Observability**: a per-item state machine with a full event history, a
  `/dashboard`, Prometheus `/metrics` and JSON logs.

```
discovered → probed → audio_extracted → transcribed → embedded → cataloged
                    (failed | skipped | needs_review at any step)
```

## Quick start

```bash
cp .env.example .env              # set passwords + JELLYFIN_API_KEY
docker compose build
docker compose up -d
docker compose exec ollama ollama pull qwen3:14b
docker compose exec ollama ollama pull nomic-embed-text
docker compose exec aibrarian-worker aibrarian prefect-limits   # GPU concurrency limits
open http://z8g4:8790/dashboard    # status
open http://z8g4:4200              # Prefect UI
```

Want a step-by-step checklist? See **[docs/ROADMAP.md](docs/ROADMAP.md)**.

## Layout

| Path | What |
|---|---|
| `src/aibrarian/` | the package (see [architecture](docs/architecture.md)) |
| `src/aibrarian/migrations/` | SQL migrations, applied automatically |
| `docker/` | Dockerfiles, Postgres init, Prometheus scrape config, Grafana dashboard |
| `docker-compose.yml` | the stack (profile `devtools`: DeepWiki + Code-Graph-RAG) |
| `docs/` | architecture, status model, migration, operations, devtools, roadmap |
| `tests/` | pytest suite (external services mocked; optional real-Postgres tests) |

## CLI

```bash
aibrarian status                  # stage counts, backlog, ETA
aibrarian timeline 42             # event history of item 42
aibrarian run-action probe --id 42
aibrarian pipeline                # all stages once, without Prefect
aibrarian library-scan --max-items 50
aibrarian book-scan
aibrarian apply --domain video --kind movie            # dry-run plan
aibrarian apply --domain video --kind movie --apply    # actually move
aibrarian search "space travel" --mode semantic
aibrarian clip "I'll be back" --title "Terminator"
aibrarian srt-report | sync-subtitles | cleanup-skipped | srt-catalog
aibrarian openlibrary-import /data/openlibrary/ol_dump_editions.txt.gz
aibrarian gen-docs src --out docs/reference
```

Anything that changes files is a **dry run unless you pass `--apply`**.

## Development

```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev,flows,media]"
ruff check . && pytest
# optional real-Postgres tests:
AIB_TEST_DATABASE_URL=postgresql://postgres@localhost:5432/aibtest pytest
```

## Docs

- [Architecture](docs/architecture.md): components, data flow, design decisions (why Neo4j and Redis were dropped)
- [Status model](docs/status-model.md): state machine, events table, retries, metrics
- [Migration from the old stack](docs/migration.md): old script to new module, task or flow
- [Operations](docs/operations.md): day-2 runbook
- [Devtools](docs/devtools.md): DeepWiki + Code-Graph-RAG
- [Roadmap / deployment checklist](docs/ROADMAP.md)
