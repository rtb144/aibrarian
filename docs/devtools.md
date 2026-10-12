# Developer tooling (compose profile `devtools`)

Two optional tools for understanding and maintaining this codebase. Both run
against the repo checkout mounted read-only, and use the stack's Ollama (GPU1)
where they support it. Ports bind to `127.0.0.1` only; use an SSH tunnel
from your laptop.

```bash
docker compose --profile devtools up -d deepwiki memgraph memgraph-lab codegraph-mcp
docker compose --profile devtools run --rm codegraph-index      # build/refresh the code graph
```

Verified against the projects' repos on 2026-10-11. Anything marked
**TODO(verify)** is a reasonable configuration I could not run here (no
Docker or GPU in the build sandbox).

## DeepWiki (deepwiki-open)

[AsyncFuncAI/deepwiki-open](https://github.com/AsyncFuncAI/deepwiki-open)
generates an interactive wiki (pages and diagrams) for a repository, with RAG
Q&A over the code.

| | |
|---|---|
| Image | `ghcr.io/asyncfuncai/deepwiki-open:latest` (published on GHCR; tags `latest`, `main`, `sha-*`) |
| Ports | API `8001`, UI `3000` in the container → host `127.0.0.1:8001` and **`127.0.0.1:3001`** (z8g4's Grafana already uses 3000) |
| Ollama | `DEEPWIKI_EMBEDDER_TYPE=ollama` selects the `embedder_ollama` config (`nomic-embed-text`), and `OLLAMA_HOST=http://ollama:11434` (both read in `api/config.py` and `api/rag/rag.py`) |
| Data | `deepwiki_data:/root/.adalflow` (cloned repos, embeddings, wiki cache) |
| Repo | `./` mounted at `/app/local-repos/aibrarian`; in the UI, enter the local path `/app/local-repos/aibrarian` |

Usage:
1. `ssh -L 3001:127.0.0.1:3001 -L 8001:127.0.0.1:8001 z8g4`, then open http://localhost:3001
2. Enter `/app/local-repos/aibrarian`, tick **Local Ollama Model**, choose the
   model (`qwen3:14b` is available in our Ollama; DeepWiki's default list
   suggests `qwen3:1.7b`), and generate.

Notes / TODO(verify):
- Upstream documents Ollama mainly through `Dockerfile-ollama-local`, an image that bundles its own Ollama. We use the standard image with an external Ollama. The env vars above are what the code reads, but this exact combination hasn't been run here.
- `qwen3:14b` needs to be added as a custom model in the UI (the Ollama provider has `supportsCustomModel: true`).
- The upstream README has been replaced by a "Grok Wiki 2.0" notice. The code and images are still current, so pin a `sha-*` tag once you have a working one.

## Code-Graph-RAG ("CodeGraph")

[vitali87/code-graph-rag](https://github.com/vitali87/code-graph-rag) parses
the repo with Tree-sitter into a **Memgraph** knowledge graph (modules,
classes, functions, calls). You can then query it in plain English, or expose
it to editors and agents as an **MCP server**.

| | |
|---|---|
| Images | `ghcr.io/vitali87/code-graph-rag:latest` (entrypoint `code-graph-rag`, default `mcp-server`); `memgraph/memgraph-mage:3.3` with `--telemetry-enabled=false`; `memgraph/lab:3.13.2` (the versions upstream pins in `codebase_rag/docker-compose.yaml`) |
| Index | `codegraph-index` runs `start --repo-path /repo --update-graph`, which syncs the graph and exits |
| MCP | `codegraph-mcp` runs `mcp-server --transport http --host 0.0.0.0 --port 8080` → host `127.0.0.1:8085` |
| Graph UI | Memgraph Lab on `127.0.0.1:3002` (quick-connect host `memgraph`) |
| Ollama | `ORCHESTRATOR_PROVIDER=ollama`, `CYPHER_PROVIDER=ollama`, `*_ENDPOINT=http://ollama:11434/v1`, `*_MODEL=${CGR_MODEL:-qwen3:14b}` (names from upstream `.env.example`) |
| Memgraph | `MEMGRAPH_HOST=memgraph`, `MEMGRAPH_PORT=7687` |

Usage:
1. `docker compose --profile devtools run --rm codegraph-index`. Re-run it after big changes.
2. Explore at http://localhost:3002 (tunnel), e.g. `MATCH (f:Function)-[:CALLS]->(g) RETURN f,g LIMIT 50`.
3. Point an MCP-capable editor or agent at `http://localhost:8085` (tunnel).
4. Interactive Q&A: `docker compose --profile devtools run --rm codegraph-index start --repo-path /repo`

Notes / TODO(verify):
- The MCP HTTP path suffix (e.g. `/mcp`) isn't documented in what I checked. Confirm it from the container logs on first start.
- Upstream recommends coder models (e.g. `qwen2.5-coder`) for Cypher generation. If `qwen3:14b` writes poor Cypher, `ollama pull qwen2.5-coder:14b` and set `CGR_MODEL`.
- Memgraph's Bolt port 7687 is the same port the old Neo4j used. Stop the old stack before starting the devtools profile.
- The semantic code-search extra (`[semantic]`, which uses Qdrant) is not in the published image. Graph queries work without it.

## Why both?

- **DeepWiki** is for reading: narrative docs and diagrams for onboarding or a refresher.
- **Code-Graph-RAG** is for precise questions: "who calls `run_action`?", "what touches `aib.media_event`?", impact analysis before a refactor, and MCP access for coding agents.
- `aibrarian gen-docs` (the old `gen_docs.py`) still exists for plain Markdown reference pages in an Obsidian vault.

## LAN access
Devtools ports (3001 DeepWiki UI, 8001 DeepWiki API, 3002 Memgraph Lab, 8085 Code-Graph-RAG MCP) are published on all interfaces for LAN use, e.g. http://192.168.68.52:3001. Memgraph Bolt (7687/7444) stays on 127.0.0.1. There is no auth on these UIs; keep them off the internet.

Code-Graph-RAG MCP refuses to bind to 0.0.0.0 without a token: set `MCP_HTTP_AUTH_TOKEN` in `.env` (e.g. `openssl rand -hex 32`); MCP clients must send it as a bearer token.
