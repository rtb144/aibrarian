"""Open Library editions: dump import + local lookup (successor of
import_openlibrary_pg.py and the DuckDB openlibrary_catalog.py service).

Dump format (``ol_dump_editions_*.txt[.gz]``): tab-separated
``type  key  revision  last_modified  json``.
"""

from __future__ import annotations

import gzip
import json
import re
import unicodedata
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from rapidfuzz import fuzz


def remove_nulls(value: Any) -> Any:
    """Strip NUL bytes (Postgres rejects them in text/jsonb)."""
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, list):
        return [remove_nulls(v) for v in value]
    if isinstance(value, dict):
        return {remove_nulls(k): remove_nulls(v) for k, v in value.items()}
    return value


def norm(value: str | None) -> str:
    if not value:
        return ""
    v = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", v.lower())).strip()


def _year(s: str | None) -> int | None:
    m = re.search(r"\b(1[5-9]\d{2}|20\d{2})\b", s or "")
    return int(m.group(1)) if m else None


def parse_dump_line(raw: bytes | str) -> tuple | None:
    line = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
    parts = line.rstrip("\n").split("\t")
    if len(parts) < 5 or parts[0] != "/type/edition":
        return None
    try:
        rec = remove_nulls(json.loads(parts[4]))
    except json.JSONDecodeError:
        return None
    title = rec.get("title")
    if not title:
        return None
    if rec.get("subtitle"):
        title = f"{title}: {rec['subtitle']}"
    authors = [a.get("key", "") for a in rec.get("authors") or [] if isinstance(a, dict)]
    by = rec.get("by_statement")
    if by:
        authors = [by] + authors
    return (parts[1], title, norm(title), authors,
            (rec.get("isbn_10") or [None])[0], (rec.get("isbn_13") or [None])[0],
            _year(rec.get("publish_date")), rec.get("publishers") or [], json.dumps(rec))


def iter_dump(path: Path) -> Iterator[bytes]:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rb") as fh:
        yield from fh


def import_dump(dump: Path, dsn: str, batch_lines: int = 100_000,
                truncate: bool = False, log=print) -> tuple[int, int]:
    import psycopg

    inserted = skipped = 0
    cols = ("key", "title", "title_norm", "authors", "isbn10", "isbn13", "publish_year",
            "publishers", "data")
    with psycopg.connect(dsn) as c:
        if truncate:
            c.execute("TRUNCATE aib.ol_edition")
        c.execute("CREATE TEMP TABLE ol_stage (LIKE aib.ol_edition) ON COMMIT DELETE ROWS")
        batch: list[tuple] = []

        def flush() -> None:
            nonlocal batch
            with c.cursor().copy(f"COPY ol_stage ({','.join(cols)}) FROM STDIN") as cp:
                for row in batch:
                    cp.write_row(row)
            c.execute("INSERT INTO aib.ol_edition SELECT * FROM ol_stage "
                      "ON CONFLICT (key) DO UPDATE SET "
                      + ", ".join(f"{k}=EXCLUDED.{k}" for k in cols[1:]))
            c.commit()
            batch = []

        for n, raw in enumerate(iter_dump(dump), 1):
            row = parse_dump_line(raw)
            if row is None:
                skipped += 1
            else:
                batch.append(row)
                inserted += 1
            if len(batch) >= batch_lines:
                flush()
                log(f"committed lines={n:,} inserted={inserted:,} skipped={skipped:,}")
        if batch:
            flush()
    log(f"done inserted={inserted:,} skipped={skipped:,}")
    return inserted, skipped


def score_match(title: str, author: str | None, row: dict[str, Any]) -> float:
    s = fuzz.token_set_ratio(norm(title), row["title_norm"]) / 100
    if author and row.get("authors"):
        a = max(fuzz.token_set_ratio(norm(author), norm(x)) for x in row["authors"]) / 100
        s = 0.75 * s + 0.25 * a
    return round(s, 3)


def lookup(db, title: str | None = None, author: str | None = None,
           isbn: str | None = None, limit: int = 5) -> list[dict[str, Any]]:
    """ISBN exact match (confidence 1.0) else trigram title candidates re-ranked."""
    with db.conn() as c:
        if isbn:
            digits = re.sub(r"[^0-9Xx]", "", isbn)
            rows = c.execute("SELECT key, title, title_norm, authors, isbn10, isbn13, "
                             "publish_year FROM aib.ol_edition WHERE isbn13=%s OR isbn10=%s "
                             "LIMIT %s", (digits, digits, limit)).fetchall()
            if rows:
                return [r | {"match_type": "isbn", "confidence": 1.0} for r in rows]
        if not title:
            return []
        rows = c.execute("SELECT key, title, title_norm, authors, isbn10, isbn13, publish_year "
                         "FROM aib.ol_edition WHERE title_norm %% %s "
                         "ORDER BY similarity(title_norm, %s) DESC LIMIT 50",
                         (norm(title), norm(title))).fetchall()
    scored = [r | {"match_type": "title", "confidence": score_match(title, author, r)}
              for r in rows]
    return sorted(scored, key=lambda r: r["confidence"], reverse=True)[:limit]


def make_ol_lookup(db):
    """Adapter for :func:`aibrarian.books.catalog_file`."""
    def _lookup(title, author, isbn):
        try:
            hits = lookup(db, title, author, isbn, limit=1)
        except Exception:
            return None
        if not hits:
            return None
        h = hits[0]
        return {"title": h["title"], "author": (h["authors"] or [None])[0],
                "year": h["publish_year"], "source": "openlibrary", "key": h["key"]}
    return _lookup
