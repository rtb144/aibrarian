# Roadmap: deploy aibrarian and retire the old stack

How to use this: do **one box at a time**. Each step is about 15–30 min,
with one action and one way to check it's done. You can stop after any
step, because nothing breaks the old stack until Phase 5. Tick boxes as
you go (edit this file, or copy it into Obsidian).

> Conventions: run commands **on z8g4** unless the step says otherwise.
> `dc` = `docker compose`. The new stack lives in `/opt/stacks/aibrarian`.

---

## Phase 0: Prep (nothing changes yet)

- [ ] **0.1 Check GPU driver.**
  Do: `nvidia-smi`
  Done when: both GPUs are listed and "CUDA Version" is **12.8 or higher**.

- [ ] **0.2 Check disk space.**
  Do: `df -h /var/lib/docker /opt`
  Done when: at least **40 GB free** on the Docker disk (images + models).

- [ ] **0.3 Note the old stack's state.**
  Do: `cd /opt/stacks/ai && docker compose ps > ~/old-stack-ps.txt`
  Done when: `~/old-stack-ps.txt` exists and lists the old containers.

- [ ] **0.4 Back up the old catalog DB (safety net).**
  Do: `docker exec prefect-postgres pg_dumpall -U prefect > ~/old-postgres-$(date +%F).sql`
  Done when: the file exists and is bigger than 1 MB (`ls -lh ~/old-postgres-*`).

- [ ] **0.5 Get a Jellyfin API key.**
  Do: Jellyfin → Dashboard → API Keys → **+** → name it `aibrarian`.
  Done when: the key is saved in your password manager.

## Phase 1: Get the code onto z8g4

- [ ] **1.1 Clone the repo.**
  Do: `sudo mkdir -p /opt/stacks/aibrarian && sudo chown $USER /opt/stacks/aibrarian && git clone https://github.com/rtb144/aibrarian /opt/stacks/aibrarian`
  Done when: `ls /opt/stacks/aibrarian/docker-compose.yml` prints the path.

- [ ] **1.2 Create your `.env`.**
  Do: `cd /opt/stacks/aibrarian && cp .env.example .env && nano .env` → set `POSTGRES_PASSWORD` (new, random) and `JELLYFIN_API_KEY`.
  Done when: `grep -c change-me .env` prints `0`.

- [ ] **1.3 Set file ownership IDs.**
  Do: `stat -c '%u:%g' /opt/jellyfin/media/01-movies`, then put those numbers in `AIB_UID` / `AIB_GID` in `.env`.
  Done when: `.env` has the same numbers that `stat` printed.

- [ ] **1.4 Create the data folder.**
  Do: `sudo mkdir -p /opt/stacks/aibrarian/data && sudo chown $(grep AIB_UID .env | cut -d= -f2):$(grep AIB_GID .env | cut -d= -f2) /opt/stacks/aibrarian/data`
  Done when: `ls -ld /opt/stacks/aibrarian/data` shows that owner.

- [ ] **1.5 Validate the compose file.**
  Do: `dc config -q`
  Done when: it prints **nothing** (no errors).

## Phase 2: Build images (old stack still running)

- [ ] **2.1 Build the app image.**
  Do: `dc build aibrarian-api`
  Done when: it ends with `Built` / `naming to aibrarian:latest`.

- [ ] **2.2 Build the GPU image** (slow, ~15 min).
  Do: `dc build aibrarian-gpu`
  Done when: `docker images | grep aibrarian-gpu` shows it.

- [ ] **2.3 Smoke-test the CLI.**
  Do: `docker run --rm aibrarian:latest aibrarian --help`
  Done when: you see the list of commands (`status`, `pipeline`, …).

- [ ] **2.4 Smoke-test CUDA in the GPU image.**
  Do: `docker run --rm --gpus '"device=0"' aibrarian-gpu:latest python -c "import ctranslate2; print(ctranslate2.get_cuda_device_count())"`
  Done when: it prints `1`.

## Phase 3: Switch over (short downtime for search and transcription)

- [ ] **3.1 Stop the old AI stack.**
  Do: `cd /opt/stacks/ai && docker compose stop`
  Done when: `docker compose ps` shows nothing running. (Jellyfin is in a different stack and keeps running.)

- [ ] **3.2 Start the databases.**
  Do: `cd /opt/stacks/aibrarian && dc up -d postgres qdrant`
  Done when: `dc ps` shows both as `healthy` (wait about a minute).

- [ ] **3.3 Start Ollama on GPU1.**
  Do: `dc up -d ollama`
  Done when: `nvidia-smi` shows an `ollama` process on **GPU 1** (it may show only after the next step).

- [ ] **3.4 Pull the models.**
  Do: `dc exec ollama ollama pull qwen3:14b && dc exec ollama ollama pull nomic-embed-text`
  Done when: `dc exec ollama ollama list` shows both models.
  Tip: to reuse the old models instead, stop here and copy the old `ollama` volume's contents into `aibrarian_ollama_models`.

- [ ] **3.5 Start Prefect.**
  Do: `dc up -d prefect-server`
  Done when: http://z8g4:4200 opens in your browser.

- [ ] **3.6 Start the API.**
  Do: `dc up -d aibrarian-api`
  Done when: `curl -s z8g4:8790/health` shows `"status":"ok"` and `"jellyfin":true`.

- [ ] **3.7 Check the dashboard.**
  Do: open http://z8g4:8790/dashboard
  Done when: the page loads and shows stage cards (all 0 for now).

- [ ] **3.8 Start the workers.**
  Do: `dc up -d aibrarian-worker aibrarian-gpu`
  Done when: Prefect UI → **Deployments** lists `media-pipeline`, `discover`, `library-scan`, `book-scan`, `retry-failed`, `openlibrary-import` and `stage`.

- [ ] **3.9 Set the GPU limits.**
  Do: `dc exec aibrarian-worker aibrarian prefect-limits`
  Done when: Prefect UI → **Concurrency** shows `gpu-whisper = 1`.

## Phase 4: First runs and checks

- [ ] **4.1 Trigger the pipeline once.**
  Do: Prefect UI → Deployments → `media-pipeline` → **Run**.
  Done when: the run finishes and the dashboard's `discovered` count is above 0.

- [ ] **4.2 Watch one movie go all the way.**
  Do: wait for the next 2–3 runs (5 min apart), then click a `cataloged` card on the dashboard and open one row's timeline.
  Done when: you see 6 events: `discover` → `probe` → `extract_audio` → `transcribe` → `embed` → `catalog`.

- [ ] **4.3 Confirm old transcripts are reused.**
  Do: in that timeline, open the `transcribe` event.
  Done when: details show `"reused_sidecar": true` for a movie the old stack had already transcribed.

- [ ] **4.4 Confirm whisper is on GPU0.**
  Do: during a run, `nvidia-smi`
  Done when: a python process is on **GPU 0** and ollama is on **GPU 1**.

- [ ] **4.5 Try search.**
  Do: `curl 'z8g4:8790/search?q=hello&limit=3'`
  Done when: you get JSON hits with `path`, `start` and `text`.

- [ ] **4.6 Try a clip.**
  Do: pick a line from 4.5, then `curl -X POST z8g4:8790/clip -H 'content-type: application/json' -d '{"phrase":"<the line>"}'`
  Done when: a new `.mp4` is in `/opt/stacks/aibrarian/data/clips/` and it plays.

- [ ] **4.7 Run the library scan (dry run, small).**
  Do: `curl -X POST 'z8g4:8790/scan?max_items=20'`, wait 5 min, then `curl z8g4:8790/report/latest | jq '.summary'`
  Done when: `processed` is 20.

- [ ] **4.8 Review 5 suggestions.**
  Do: `curl 'z8g4:8790/suggestions?domain=video' | jq '.[0:5] | .[] | {source_path,target_path,confidence,decision}'`
  Done when: you've looked at 5 and they make sense (just note any that look wrong).

- [ ] **4.9 Run the book scan.**
  Do: `curl -X POST z8g4:8790/book-scan -H 'content-type: application/json' -d '{"max_items":10}'`
  Done when: `curl z8g4:8790/book-suggestions | jq length` is above 0.

- [ ] **4.10 Import Open Library (runs about 1–2 h in the background).**
  Do: Prefect UI → `openlibrary-import` → Run → set `dump_path` to `/data/openlibrary/<your dump file>`.
  Done when: `curl z8g4:8790/openlibrary/stats` shows a big `editions` number.

- [ ] **4.11 Hook up Prometheus.**
  Do: append `docker/prometheus/aibrarian-scrape.yml` to the `scrape_configs` in `/opt/stacks/monitoring`'s prometheus.yml, then restart Prometheus.
  Done when: Prometheus → Status → Targets shows the 3 aibrarian targets **UP**.

- [ ] **4.12 Import the Grafana dashboard.**
  Do: Grafana → Dashboards → Import → upload `docker/grafana/aibrarian-dashboard.json`.
  Done when: the "aibrarian" dashboard shows stage bars.

## Phase 5: Apply library changes (carefully)

- [ ] **5.1 Plan movie moves (dry run).**
  Do: `dc exec aibrarian-api aibrarian apply --domain video --kind movie > ~/plan-movies.json`
  Done when: you've skimmed the file. Every op says `dry_run`.

- [ ] **5.2 Apply 1 movie move.**
  Do: pick one id from the plan, then `dc exec aibrarian-api aibrarian apply --id <id> --apply`
  Done when: the movie and its `.srt` are in the new folder **and** Jellyfin still plays it (after a library scan).

- [ ] **5.3 Apply the rest of the confident movie moves.**
  Do: `dc exec aibrarian-api aibrarian apply --domain video --kind movie --apply`
  Done when: the output has no `"result": "error"` (`skip_dst_exists` is fine; that's handled in 5.4).

- [ ] **5.4 Clean up duplicates (dry run first).**
  Do: `dc exec aibrarian-api aibrarian cleanup-skipped`, check the output, then add `--apply`.
  Done when: the dry-run list looked right and the `--apply` run shows `ok` rows.

- [x] **5.4b Quarantine identical duplicates (first batch: 3 files, 50.74 GB, still playable).**
  Do: `dc exec aibrarian-api aibrarian duplicates quarantine --limit 3` then `--execute`.

- [ ] **5.4c Let the automation run.** `dedup-quarantine` (Sun 05:00 UTC) quarantines identical
  copies in batches of `DEDUP_BATCH_LIMIT`; `quarantine-purge` (daily 04:37 UTC) deletes them
  after `QUARANTINE_RETENTION_DAYS` (30). Watch `/dedup/ui`.
  Done when: a week's runs show `quarantine` rows and no `error` rows.

- [ ] **5.5 Repair old orphaned subtitles.**
  Do: `dc exec aibrarian-api aibrarian srt-report --log /data/old-reports/<move_log>.jsonl` then `sync-subtitles --log … --apply`
  Done when: re-running `srt-report` shows fewer `lost_after_move` entries.

## Phase 6: Retire the old stack (after about a week of good runs)

- [ ] **6.1 Make sure the new stack has run cleanly for 7 days.**
  Do: dashboard → the `failed` card is small, and Prefect shows green runs.
  Done when: you're comfortable. If not, stop here.

- [ ] **6.2 Archive old reports.**
  Do: `tar czf ~/old-ai-reports.tgz /opt/stacks/ai/reports`
  Done when: the tarball exists.

- [ ] **6.3 Remove the old containers** (volumes stay).
  Do: `cd /opt/stacks/ai && docker compose down`
  Done when: `docker ps -a | grep -E 'movie-transcriber|neo4j|catalog-api'` prints nothing.

- [ ] **6.4 Delete the old host venv.**
  Do: `rm -rf /opt/stacks/ai/venv`
  Done when: `ls /opt/stacks/ai/venv` says "No such file".

- [ ] **6.5 Drop old volumes** (only after 6.1–6.3!).
  Do: `docker volume ls | grep '^local *ai_'`, then `docker volume rm` the neo4j, redis, qdrant and postgres ones.
  Done when: they're gone and the new stack still works (`/health` ok).

## Phase 7: Optional devtools

- [ ] **7.1 Start DeepWiki.**
  Do: `dc --profile devtools up -d deepwiki`
  Done when: http://localhost:3001 opens (through `ssh -L 3001:127.0.0.1:3001 z8g4`).

- [ ] **7.2 Generate the wiki.**
  Do: in DeepWiki, enter `/app/local-repos/aibrarian`, choose Local Ollama, and generate.
  Done when: you can open the "Overview" page.

- [ ] **7.3 Build the code graph.**
  Do: `dc --profile devtools up -d memgraph memgraph-lab && dc --profile devtools run --rm codegraph-index`
  Done when: it exits with node and relationship counts.

- [ ] **7.4 Look at the graph.**
  Do: open http://localhost:3002 (tunnel) and run `MATCH (n) RETURN count(n)`.
  Done when: the count is above 0.

---

### If something goes wrong

- Roll back fast: `cd /opt/stacks/aibrarian && dc down && cd /opt/stacks/ai && docker compose start`. The old stack's volumes are untouched until 6.5.
- Stuck? Note the step number and the last command's output, and look in [operations.md](operations.md) → Troubleshooting.
