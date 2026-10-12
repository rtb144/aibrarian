"""Book / audiobook / podcast librarian (successor of book_librarian.py).

Per file: extract embedded metadata -> look up Open Library (local Postgres)
and Google Books -> ask the LLM to classify when ambiguous -> compute a
destination (Calibre / Audiobookshelf audiobooks / podcasts) -> record a
suggestion with a decision from the confidence thresholds:

* ``>= book_min_confidence`` (0.85): ``auto`` in auto mode, else ``suggest``
* ``>= book_review_confidence`` (0.60): ``review``
* below: ``reject`` (left alone)
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Any

import httpx
from rapidfuzz import fuzz

from .config import AUDIO_EXTENSIONS, BOOK_EXTENSIONS, Settings
from .models import Decision, Suggestion
from .ollama import OllamaClient
from .probe import read_audio_tags
from .video_librarian import safe_component

BOOK_KINDS = ("ebook", "audiobook", "podcast", "other")
CLASSIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": list(BOOK_KINDS)},
        "title": {"type": ["string", "null"]},
        "author": {"type": ["string", "null"]},
        "series": {"type": ["string", "null"]},
        "year": {"type": ["integer", "null"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "notes": {"type": "string"},
    },
    "required": ["kind", "title", "author", "series", "year", "confidence", "notes"],
}
_DC = "{http://purl.org/dc/elements/1.1/}"


def media_type(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in BOOK_EXTENSIONS:
        return "ebook"
    if ext in AUDIO_EXTENSIONS:
        return "audio"
    return "other"


def epub_metadata(path: Path) -> dict[str, Any]:
    """Parse the OPF inside an EPUB with the stdlib (no ebooklib needed)."""
    with zipfile.ZipFile(path) as z:
        container = ET.fromstring(z.read("META-INF/container.xml"))
        rootfile = next(e for e in container.iter() if e.tag.endswith("rootfile"))
        opf = ET.fromstring(z.read(rootfile.attrib["full-path"]))
    def first(tag: str) -> str | None:
        el = next(opf.iter(_DC + tag), None)
        return el.text.strip() if el is not None and el.text else None
    isbn = None
    for el in opf.iter(_DC + "identifier"):
        digits = re.sub(r"[^0-9Xx]", "", el.text or "")
        if len(digits) in (10, 13):
            isbn = digits
            break
    date = first("date")
    metas = {m.attrib.get("name"): m.attrib.get("content") for m in opf.iter()
             if m.tag.endswith("meta") and m.attrib.get("name")}
    return {"series": metas.get("calibre:series"),
            "series_index": metas.get("calibre:series_index"),
            "title": first("title"), "author": first("creator"), "isbn": isbn,
            "language": first("language"), "publisher": first("publisher"),
            "year": int(date[:4]) if date and date[:4].isdigit() else None}


def pdf_metadata(path: Path) -> dict[str, Any]:
    try:
        from pypdf import PdfReader
    except ImportError:
        return {}
    info = PdfReader(str(path)).metadata or {}
    return {"title": info.get("/Title"), "author": info.get("/Author")}


def extract_metadata(path: Path) -> dict[str, Any]:
    kind = media_type(path)
    meta: dict[str, Any] = {"filename": path.name, "media_type": kind}
    try:
        if path.suffix.lower() == ".epub":
            meta |= epub_metadata(path)
        elif path.suffix.lower() == ".pdf":
            meta |= pdf_metadata(path)
        elif kind == "audio":
            tags = read_audio_tags(path)
            meta |= {"title": tags.get("album") or tags.get("title"),
                     "author": tags.get("artist"), "tags": tags}
    except Exception as exc:  # corrupt files still get filename-based handling
        meta["extract_error"] = str(exc)
    return {k: v for k, v in meta.items() if v not in (None, "")}


def google_books(title: str | None, author: str | None, isbn: str | None,
                 api_key: str = "", client: httpx.Client | None = None) -> dict | None:
    if isbn:
        q = f"isbn:{isbn}"
    elif title:
        q = f"intitle:{title}" + (f"+inauthor:{author}" if author else "")
    else:
        return None
    http = client or httpx.Client(timeout=15.0)
    params = {"q": q, "maxResults": 3} | ({"key": api_key} if api_key else {})
    try:
        r = http.get("https://www.googleapis.com/books/v1/volumes", params=params)
        r.raise_for_status()
        items = r.json().get("items") or []
    except httpx.HTTPError:
        return None
    if not items:
        return None
    v = items[0].get("volumeInfo", {})
    return {"title": v.get("title"), "author": (v.get("authors") or [None])[0],
            "year": int(v["publishedDate"][:4]) if v.get("publishedDate", "")[:4].isdigit()
            else None, "source": "google_books", "match_type": "isbn" if isbn else "title",
            "isbn": isbn}


def reconcile(meta: dict[str, Any], ref: dict[str, Any] | None) -> float:
    """Agreement between embedded metadata and a reference record (0..1)."""
    if not ref or not meta.get("title"):
        return 0.0
    t = fuzz.token_set_ratio(meta["title"], ref.get("title") or "") / 100
    if meta.get("author") and ref.get("author"):
        return round(0.7 * t + 0.3 * fuzz.token_set_ratio(meta["author"], ref["author"]) / 100, 3)
    return round(t * 0.85, 3)


def classify(ollama: OllamaClient, path: Path, meta: dict[str, Any]) -> dict[str, Any]:
    prompt = (
        "Classify this file for a home library. Return one JSON object matching the schema.\n"
        f"Schema: {CLASSIFY_SCHEMA}\nRules: ebook for epub/pdf/mobi books; audiobook for "
        "narrated books (m4b, multi-part mp3 in a book folder); podcast for episodic shows; "
        "use null when unknown, never invent authors.\n"
        f"PATH: {path}\nMETADATA: {meta}"
    )
    try:
        out = ollama.generate_json(prompt, CLASSIFY_SCHEMA)
    except Exception as exc:
        return {"kind": "other", "confidence": 0.0, "notes": f"LLM error: {exc}"}
    out["confidence"] = min(1.0, max(0.0, float(out.get("confidence") or 0)))
    if out.get("kind") not in BOOK_KINDS:
        out["kind"] = "other"
    return out


def destination(kind: str, title: str | None, author: str | None, series: str | None,
                src: Path, s: Settings) -> Path | None:
    if not title:
        return None
    t = safe_component(title)
    a = safe_component(author or "Unknown Author")
    if kind == "ebook":  # Calibre-style Author/Title/file
        return s.calibre_dir / a / t / src.name
    if kind == "audiobook":  # Audiobookshelf: Author/[Series/]Title/file
        base = s.audiobooks_dir / a
        if series:
            base = base / safe_component(series)
        return base / t / src.name
    if kind == "podcast":
        return s.podcasts_dir / safe_component(series or title) / src.name
    return None


def decide(conf: float, s: Settings) -> Decision:
    if conf >= s.book_min_confidence:
        return "auto" if s.book_mode == "auto" else "suggest"
    if conf >= s.book_review_confidence:
        return "review"
    return "reject"


def catalog_file(path: Path, s: Settings, ollama: OllamaClient,
                 ol_lookup=None, gb_lookup=google_books, run_id: int | None = None) -> Suggestion:
    meta = extract_metadata(path)
    ref = None
    if ol_lookup is not None:
        ref = ol_lookup(meta.get("title"), meta.get("author"), meta.get("isbn"))
    if ref is None:
        ref = gb_lookup(meta.get("title"), meta.get("author"), meta.get("isbn"),
                        s.google_books_api_key)
    ref_score = reconcile(meta, ref)
    if meta.get("media_type") == "ebook" and ref_score >= s.book_min_confidence:
        cls = {"kind": "ebook", "title": ref.get("title") or meta.get("title"),
               "author": ref.get("author") or meta.get("author"), "series": None,
               "confidence": ref_score, "notes": f"matched {ref.get('source')}"}
    else:
        cls = classify(ollama, path, {**meta, "reference": ref})
        if ref_score:
            cls["confidence"] = round(max(cls["confidence"], (cls["confidence"] + ref_score) / 2), 3)
    # confidence is evidence-based (bookmeta.score), not the LLM's self-rating
    from .bookmeta import evaluate

    ev = evaluate({"source_path": str(path), "media_kind": cls["kind"],
                   "metadata": {"embedded": meta, "reference": ref, "classification": cls}})
    cls["llm_confidence"] = cls.get("confidence")
    cls["confidence"] = ev["confidence"]
    dst = destination(cls["kind"], ev["title"] or cls.get("title"), ev["author"],
                      ev["series"], path, s)
    conf = cls["confidence"] if dst else min(cls["confidence"], s.book_review_confidence - 0.01)
    return Suggestion(domain="book", source_path=str(path), target_path=str(dst) if dst else None,
                      metadata={"embedded": meta, "reference": ref, "classification": cls},
                      confidence=round(conf, 3), decision=decide(conf, s),
                      rationale=cls.get("notes"), media_kind=cls["kind"],
                      op="move" if dst else "none", run_id=run_id)


def iter_book_files(dirs: list[Path]):
    exts = BOOK_EXTENSIONS | AUDIO_EXTENSIONS
    for d in dirs:
        if not d.is_dir():
            continue
        for p in sorted(d.rglob("*")):
            if p.is_file() and not p.name.startswith(".") and p.suffix.lower() in exts:
                yield p
