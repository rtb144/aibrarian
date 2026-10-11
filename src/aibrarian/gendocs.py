"""LLM-written Markdown reference docs (successor of gen_docs.py).

Fixes the problems visible in the old gen_docs.log:
* it walked ``/opt/stacks`` and documented 500+ files of ``venv/site-packages``
  (neo4j driver internals) for hours -> vendored/virtualenv dirs are excluded
  and only explicitly given paths are scanned;
* runs were interrupted with no final line -> every run ends with a summary
  line (``done ok=N failed=M skipped=K``) even on Ctrl-C / errors;
* unchanged files were regenerated each time -> a content-hash cache skips them;
* compose discovery took 18 minutes -> discovery is a bounded ``os.walk``
  with the same excludes.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from collections.abc import Iterable, Iterator
from pathlib import Path

from .ollama import OllamaClient

log = logging.getLogger("aibrarian.gendocs")
EXCLUDE_DIRS = {".git", ".venv", "venv", "env", "site-packages", "node_modules", "__pycache__",
                ".mypy_cache", ".pytest_cache", ".ruff_cache", "dist", "build", ".tox"}
COMPOSE_NAMES = {"docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"}


def iter_sources(paths: Iterable[Path], suffixes: tuple[str, ...] = (".py",)) -> Iterator[Path]:
    for base in paths:
        if base.is_file():
            if base.suffix in suffixes:
                yield base
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(d for d in dirnames
                                 if d not in EXCLUDE_DIRS and not d.startswith("."))
            for fn in sorted(filenames):
                if fn.endswith(suffixes):
                    yield Path(dirpath) / fn


def discover_compose(paths: Iterable[Path]) -> list[Path]:
    out = []
    for base in paths:
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS and not d.startswith(".")]
            out += [Path(dirpath) / f for f in filenames if f in COMPOSE_NAMES]
    return sorted(out)


def frontmatter(title: str, tag: str = "ai-stack") -> str:
    return f"---\ntitle: {title}\ntags:\n  - {tag}\naliases: []\n---\n\n"


PROMPT = """Write an Obsidian Markdown technical reference for this Python file.
Sections: Purpose, Service wiring, Environment variables (table), Functions and classes,
External dependencies, Data flow, Usage and operations. Only state what the code and
compose context show; write "not visible in code" otherwise. No preamble.

FILE: {name}
```python
{code}
```
COMPOSE CONTEXT:
{compose}
"""


def generate(paths: list[Path], out_dir: Path, ollama: OllamaClient,
             compose_files: list[Path] | None = None, max_chars: int = 60_000,
             force: bool = False) -> dict[str, int]:
    out_dir.mkdir(parents=True, exist_ok=True)
    cache_file = out_dir / ".gendocs-cache.json"
    cache = json.loads(cache_file.read_text()) if cache_file.exists() else {}
    compose = "\n\n".join(p.read_text(errors="ignore")[:20_000] for p in compose_files or [])
    stats = {"ok": 0, "failed": 0, "skipped": 0}
    try:
        for src in iter_sources(paths):
            code = src.read_text(errors="ignore")
            digest = hashlib.sha256(code.encode()).hexdigest()
            if not force and cache.get(str(src)) == digest:
                stats["skipped"] += 1
                continue
            log.info("generating", extra={"file": str(src)})
            try:
                text = ollama.chat_text(PROMPT.format(name=src.name, code=code[:max_chars],
                                                      compose=compose or "(none)"))
            except Exception as exc:
                stats["failed"] += 1
                log.error("generation failed", extra={"file": str(src), "error": str(exc)})
                continue
            dst = out_dir / f"{src.stem}.md"
            dst.write_text(frontmatter(src.stem) + text.strip() + "\n", encoding="utf-8")
            cache[str(src)] = digest
            stats["ok"] += 1
            log.info("wrote", extra={"file": str(dst), "chars": len(text)})
    finally:
        cache_file.write_text(json.dumps(cache, indent=1))
        log.info("done", extra=stats)
    return stats
