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

import logging
import os
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


def quiet_pypdf() -> None:
    """pypdf logs 'Ignoring wrong pointing object N 0 (offset 0)' once per object on PDFs
    with broken xref tables - thousands of lines per file. Only errors get through."""
    for name in ("pypdf", "pypdf._reader", "pypdf.generic", "pypdf._page"):
        logging.getLogger(name).setLevel(logging.ERROR)



def quiet_http() -> None:
    """httpx logs each request URL at INFO, query string (API key) included: WARNING only,
    plus a redacting filter in case someone turns it back up."""
    from .logging_setup import RedactFilter

    for name in ("httpx", "httpcore"):
        lg = logging.getLogger(name)
        lg.setLevel(logging.WARNING)
        if not any(isinstance(f, RedactFilter) for f in lg.filters):
            lg.addFilter(RedactFilter())


quiet_pypdf()
quiet_http()


def mupdf():
    """The PyMuPDF module (``import pymupdf``; ``fitz`` for old releases) with MuPDF's own
    stderr messages switched off (warnings stay readable via TOOLS.mupdf_warnings())."""
    try:
        import pymupdf as m
    except ImportError:
        import fitz as m  # pymupdf < 1.24.3
    try:
        m.TOOLS.mupdf_display_errors(False)
        m.TOOLS.mupdf_display_warnings(False)
    except Exception:
        pass
    return m


def _mupdf_warnings(m) -> str | None:
    try:
        w = m.TOOLS.mupdf_warnings(reset=True)
    except Exception:
        return None
    return w[:300] if w else None


def _pymupdf_metadata(path: Path) -> dict[str, Any]:
    m = mupdf()  # C xref repair: fast even on broken PDFs
    with m.open(str(path)) as doc:
        info = doc.metadata or {}
        pages = doc.page_count
    return clean_metadata({"title": info.get("title"), "author": info.get("author"),
                           "subject": info.get("subject"), "keywords": info.get("keywords"),
                           "pages": str(pages) if pages else None,
                           "pdf_warnings": _mupdf_warnings(m), "pdf_reader": "pymupdf"})


def _pypdf_metadata(path: Path) -> dict[str, Any]:
    try:
        from pypdf import PdfReader
    except ImportError:
        return {}
    quiet_pypdf()
    reader = PdfReader(str(path))
    info = reader.metadata or {}
    return clean_metadata({"title": info.get("/Title"), "author": info.get("/Author"),
                           "subject": info.get("/Subject"), "keywords": info.get("/Keywords"),
                           "pdf_reader": "pypdf"})


def pdf_metadata(path: Path) -> dict[str, Any]:
    """PDF info dict + page count via PyMuPDF; pypdf only if pymupdf is missing or raises."""
    try:
        return _pymupdf_metadata(path)
    except ImportError:
        pass
    except Exception as exc:
        out = _pypdf_metadata(path)
        return out | {"pymupdf_error": str(exc)[:300]}
    return _pypdf_metadata(path)


_EXTH = {100: "author", 101: "publisher", 103: "description", 104: "isbn", 105: "subject",
         106: "date", 113: "asin", 503: "title", 524: "language"}


def mobi_metadata(path: Path) -> dict[str, Any]:
    """Title/author/ISBN/publisher from a MOBI/AZW3 (PalmDB record 0: MOBI header full name
    + EXTH records). Pure Python, reads only the first record."""
    import struct

    with open(path, "rb") as fh:
        head = fh.read(78)
        if len(head) < 78 or head[60:68] not in (b"BOOKMOBI", b"TEXtREAd"):
            raise ValueError("not a MOBI/PalmDB file")
        n = struct.unpack(">H", head[76:78])[0]
        offs = fh.read(8 * min(n, 2))
        r0 = struct.unpack(">I", offs[:4])[0]
        r1 = struct.unpack(">I", offs[8:12])[0] if n > 1 else r0 + 65536
        fh.seek(r0)
        rec = fh.read(max(0, min(r1 - r0, 1 << 20)))
    if rec[16:20] != b"MOBI":
        return {}
    hlen, enc = struct.unpack(">I", rec[20:24])[0], struct.unpack(">I", rec[28:32])[0]
    codec = "utf-8" if enc == 65001 else "cp1252"
    name_off, name_len = struct.unpack(">II", rec[84:92])
    out: dict[str, Any] = {"title": rec[name_off:name_off + name_len].decode(codec, "replace")}
    flags = struct.unpack(">I", rec[128:132])[0] if len(rec) >= 132 else 0
    e = 16 + hlen
    if flags & 0x40 and rec[e:e + 4] == b"EXTH":
        count = struct.unpack(">I", rec[e + 8:e + 12])[0]
        pos, authors = e + 12, []
        for _ in range(min(count, 500)):
            if pos + 8 > len(rec):
                break
            typ, ln = struct.unpack(">II", rec[pos:pos + 8])
            if ln < 8:
                break
            val = rec[pos + 8:pos + ln].decode(codec, "replace").strip("\x00 ")
            key = _EXTH.get(typ)
            if key == "author":
                authors.append(val)
            elif key == "title" and val:
                out["title"] = val  # EXTH 503 (updated title) beats the header full name
            elif key and val and key != "description":
                out.setdefault(key, val)
            pos += ln
        if authors:
            out["author"] = "; ".join(dict.fromkeys(authors))
    if out.get("isbn"):
        d = re.sub(r"[^0-9Xx]", "", out["isbn"])
        out["isbn"] = d if len(d) in (10, 13) else None
    date = out.pop("date", None)
    if date and date[:4].isdigit():
        out["year"] = int(date[:4])
    out.pop("asin", None)
    return out


def clean_metadata(meta: dict[str, Any]) -> dict[str, Any]:
    """Resolve IndirectObjects and coerce every scalar value to a clean str (or None)."""
    from .olindex import as_str
    out: dict[str, Any] = {}
    for k, v in meta.items():
        if isinstance(v, dict):
            out[k] = clean_metadata(v)
        elif isinstance(v, (int, float)) and not isinstance(v, bool) and k in ("year",):
            out[k] = v
        else:
            out[k] = as_str(v)
    return out


def extract_metadata(path: Path, content_scan: bool = True) -> dict[str, Any]:
    """Embedded metadata; when it is weak (see contentscan.weak_reasons) the book's own
    first/last pages are scanned too (content_isbn / content_title / content_author /
    content_snippet, capped by AIB_BOOK_CONTENT_SCAN_S, default 15s)."""
    from . import contentscan

    kind = media_type(path)
    meta: dict[str, Any] = {"filename": path.name, "media_type": kind}
    try:
        if path.suffix.lower() == ".epub":
            meta |= clean_metadata(epub_metadata(path))
        elif path.suffix.lower() == ".pdf":
            meta |= pdf_metadata(path)
        elif path.suffix.lower() in (".mobi", ".azw3", ".azw", ".prc"):
            meta |= clean_metadata(mobi_metadata(path))
        elif kind == "audio":
            tags = read_audio_tags(path)
            meta |= {"title": tags.get("album") or tags.get("title"),
                     "author": tags.get("artist"), "tags": tags}
            meta |= clean_metadata({k: meta.get(k) for k in ("title", "author")})
    except Exception as exc:  # corrupt files still get filename-based handling
        meta["extract_error"] = str(exc)
    if kind == "ebook":
        nt, na = split_filename(path.name)
        meta |= {"name_title": nt, "name_author": na}
    if content_scan and path.suffix.lower() in (".pdf", ".epub"):
        weak = contentscan.weak_reasons(meta)
        if weak:
            try:
                budget = float(os.environ.get("AIB_BOOK_CONTENT_SCAN_S") or 15)
            except ValueError:
                budget = 15.0
            meta |= contentscan.scan(path, budget) | {"weak_metadata": weak}
    return {k: v for k, v in meta.items() if v not in (None, "")}


_NAME_PUBLISHER = re.compile(r"^(wiley|wrox( press)?|o ?reilly( media)?|apress|packt( publishing)?|"
                             r"manning|addison[ -]wesley|prentice[ -]hall|mcgraw[ -]hill|"
                             r"no starch( press)?|sams|sybex|microsoft press|"
                             r"pragmatic (bookshelf|programmers))\b[ ._-]*", re.I)
_NAME_TAIL = re.compile(r"[\s._-]*(\(?(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)"
                        r"[a-z]*[ ._-]*(19|20)\d\d\)?|\(?(19|20)\d\d\)?|retail|ebook|"
                        r"\[[^\]]*\]|\((pdf|epub|ebook)\))\s*$", re.I)


def filename_title(name: str) -> str | None:
    """Readable title from a download-style file name ('Wiley.Data.Structures.and.
    Algorithms.in.Java.5th.Edition.Mar.2010.pdf' -> 'Data Structures and Algorithms in Java
    5th Edition'); None for junk names (ISBNs, digits, scan0001...)."""
    from .contentscan import title_is_garbage

    stem = Path(name).stem
    if re.fullmatch(r"[\dXx\s._-]+", stem):
        return None  # ISBN / number file name
    t = stem
    if " " not in t and t.count(".") >= 2:
        t = re.sub(r"(?<!\d)\.|\.(?!\d)", " ", t)   # dots as separators, keep 'v4.2'
    t = re.sub(r"_+", " ", t)
    t = _NAME_PUBLISHER.sub("", t)
    for _ in range(3):
        t = _NAME_TAIL.sub("", t)
    t = re.sub(r"\s+", " ", t).strip(" -._,")
    return None if title_is_garbage(t) else t


_NAME_SEP = re.compile(r"\s+[-\u2013\u2014]\s+")
_NOT_AUTHOR = re.compile(r"\d|\b(edition|vol|volume|book|part|series|introduction|guide|"
                         r"history|the|of|and the|for)\b", re.I)


def _name_author(raw: str) -> str | None:
    """'Warburton, Nigel' -> 'Nigel Warburton'; 'Krauss, Lawrence M_' -> 'Lawrence M.
    Krauss'; 'Michele Boldrin;David K. Levine' -> 'Michele Boldrin; David K. Levine'.
    None if it does not look like person names."""
    from .contentscan import looks_like_name

    raw = raw.strip(" _.-")
    names = []
    for part in re.split(r"\s*(?:;|&|\band\b)\s*", raw):
        part = re.sub(r"(?<=\b[A-Z])_", ".", part.strip(" _")).strip(" _")
        part = re.sub(r"_", " ", part)
        if part.count(",") == 1:
            last, first = (x.strip() for x in part.split(","))
            part = f"{first} {last}".strip()
        part = re.sub(r"\b([A-Z])(?=\s|$)", r"\1.", part)  # bare initial -> 'M.'
        part = re.sub(r"\s+", " ", part).strip()
        if not part or _NOT_AUTHOR.search(part) or not looks_like_name(part.replace(".", ". ")
                                                                         .replace(".  ", ". ")):
            return None
        names.append(part)
    return "; ".join(names) if names else None


def split_filename(name: str) -> tuple[str | None, str | None]:
    """('Title', 'Author') from 'Title - Author.ext' (Calibre/Kindle export style);
    '_' as ':' in titles ('Anarchism_ A Very Short Introduction'). Author must look like
    person names, otherwise the whole stem is the title."""
    stem = Path(name).stem
    parts = _NAME_SEP.split(stem)
    title, author = stem, None
    if len(parts) >= 2:
        cand = _name_author(parts[-1])
        if cand:
            title, author = " - ".join(parts[:-1]), cand
        else:
            cand = _name_author(parts[0]) if "," in parts[0] else None  # 'Last, First - Title'
            if cand and len(parts) == 2:
                title, author = parts[1], cand
    title = re.sub(r"_\s+", ": ", title)
    t = filename_title(title + ".x")
    return t, author


def name_isbns(meta: dict[str, Any]) -> list[str]:
    """Check-digit-valid ISBNs in the file name and the embedded title
    ('0321670329.pdf', 'Microsoft Word - 978-1-4302-4002-0_Smithwick_Front.doc')."""
    from .olindex import isbns_in_name

    out: list[str] = []
    for src in (meta.get("filename"), meta.get("title")):
        for i in isbns_in_name(str(src or "")):
            if i not in out:
                out.append(i)
    return out


def lookup_fields(meta: dict[str, Any]) -> dict[str, Any]:
    """Title/author/isbn to look up and their source. Title: embedded unless junk, else the
    file name (downloads are usually well named), else the content-scan title unless
    garbled. ISBN: embedded, else from the file name / embedded title, else content."""
    from .contentscan import title_is_garbage, title_is_garbled

    title, src = meta.get("title"), "embedded"
    if not title or title_is_garbage(title, meta.get("filename")):
        title, src = (meta.get("name_title") or filename_title(meta.get("filename") or "")
                      ), "filename"
        ct = meta.get("content_title")
        ct_ok = bool(ct) and not title_is_garbage(ct) and not title_is_garbled(ct)
        if ct_ok and (not title or len(title.split()) < 2):  # 'broken.pdf', 'book1.pdf'
            title, src = ct, "content"
        if not title:
            src = None
    author = meta.get("author")
    if not author or title_is_garbage(author):
        ca = meta.get("content_author")
        author = meta.get("name_author") or (ca if ca and not title_is_garbled(ca) else None)
    isbn = meta.get("isbn") or next(iter(name_isbns(meta)), None) or meta.get("content_isbn")
    return {"title": title, "author": author, "isbn": isbn, "title_source": src}


_GB_CACHE: dict[str, dict | None] = {}
_GB_BLOCKED_UNTIL = 0.0


def google_books(title: str | None, author: str | None, isbn: str | None,
                 api_key: str = "", client: httpx.Client | None = None) -> dict | None:
    """Google Books lookup (only used with AIB_GOOGLE_BOOKS_ENABLED=true). Results are
    cached per process; a 429 pauses all calls for 10 minutes instead of retrying."""
    global _GB_BLOCKED_UNTIL
    import time

    if isbn:
        q = f"isbn:{isbn}"
    elif title:
        q = f"intitle:{title}" + (f"+inauthor:{author}" if author else "")
    else:
        return None
    if q in _GB_CACHE:
        return _GB_CACHE[q]
    if time.monotonic() < _GB_BLOCKED_UNTIL:
        return None
    http = client or httpx.Client(timeout=15.0)
    params = {"q": q, "maxResults": 3} | ({"key": api_key} if api_key else {})
    try:
        r = http.get("https://www.googleapis.com/books/v1/volumes", params=params)
        if r.status_code == 429:
            _GB_BLOCKED_UNTIL = time.monotonic() + 600
            return None
        r.raise_for_status()
        items = r.json().get("items") or []
    except httpx.HTTPError:
        return None
    if not items:
        _GB_CACHE[q] = None
        return None
    v = items[0].get("volumeInfo", {})
    out = {"title": v.get("title"), "author": (v.get("authors") or [None])[0],
           "year": int(v["publishedDate"][:4]) if v.get("publishedDate", "")[:4].isdigit()
           else None, "source": "google_books", "match_type": "isbn" if isbn else "title",
           "isbn": isbn}
    _GB_CACHE[q] = out
    return out


def reconcile(meta: dict[str, Any], ref: dict[str, Any] | None) -> float:
    """Agreement between embedded metadata and a reference record (0..1)."""
    if not ref or not meta.get("title"):
        return 0.0
    t = fuzz.token_set_ratio(meta["title"], ref.get("title") or "") / 100
    if meta.get("author") and ref.get("author"):
        return round(0.7 * t + 0.3 * fuzz.token_set_ratio(meta["author"], ref["author"]) / 100, 3)
    return round(t * 0.85, 3)


def classify(ollama: OllamaClient, path: Path, meta: dict[str, Any],
             snippet: str | None = None) -> dict[str, Any]:
    prompt = (
        "Classify this file for a home library. Return one JSON object matching the schema.\n"
        f"Schema: {CLASSIFY_SCHEMA}\nRules: ebook for epub/pdf/mobi books; audiobook for "
        "narrated books (m4b, multi-part mp3 in a book folder); podcast for episodic shows; "
        "use null when unknown, never invent authors.\n"
        f"PATH: {path}\nMETADATA: {meta}"
        + (f"\nTEXT FROM THE BOOK (title/copyright pages):\n{snippet[:1500]}" if snippet else "")
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
                 ol_lookup=None, gb_lookup=google_books, run_id: int | None = None,
                 ref_lookup=None, extract=None) -> Suggestion:
    meta = dict((extract or extract_metadata)(path))
    snippet = meta.pop("content_snippet", None)  # prompt only, not stored
    lk = lookup_fields(meta)
    ref = None
    if ol_lookup is not None:
        ref = ol_lookup(lk["title"], lk["author"], lk["isbn"])
    if ref is None and getattr(s, "google_books_enabled", False):
        ref = gb_lookup(lk["title"], lk["author"], lk["isbn"], s.google_books_api_key)
    ref_score = reconcile({**meta, "title": lk["title"], "author": lk["author"]}, ref)
    if meta.get("media_type") == "ebook" and ref_score >= s.book_min_confidence:
        cls = {"kind": "ebook", "title": ref.get("title") or lk["title"],
               "author": ref.get("author") or lk["author"], "series": None,
               "confidence": ref_score, "notes": f"matched {ref.get('source')}"}
    else:
        cls = classify(ollama, path, {**meta, "reference": ref, "lookup": lk}, snippet)
        if meta.get("media_type") == "ebook" and cls.get("kind") not in ("ebook", "audiobook"):
            # a pdf/epub in a book folder is a book; the LLM must not drop it to 'other'
            cls["notes"] = f"{cls.get('notes') or ''} [kind {cls.get('kind')} -> ebook]".strip()
            cls["kind"] = "ebook"
        if ref_score:
            cls["confidence"] = round(max(cls["confidence"], (cls["confidence"] + ref_score) / 2), 3)
    from .contentscan import title_is_garbage

    if lk["title"] and (not cls.get("title") or title_is_garbage(cls.get("title"))):
        cls["title"] = lk["title"]
    if lk["author"] and not cls.get("author"):
        cls["author"] = lk["author"]
    cls["title_source"] = lk["title_source"]
    # confidence is evidence-based (bookmeta.score), not the LLM's self-rating
    from .bookmeta import evaluate

    ev = evaluate({"source_path": str(path), "media_kind": cls["kind"],
                   "metadata": {"embedded": meta, "reference": ref, "classification": cls}},
                  ref_lookup)
    cls["llm_confidence"] = cls.get("confidence")
    cls["confidence"] = ev["confidence"]
    dst = destination(cls["kind"], ev["title"] or cls.get("title"), ev["author"],
                      ev["series"], path, s)
    conf = cls["confidence"] if dst else min(cls["confidence"], s.book_review_confidence - 0.01)
    rationale = cls.get("notes")
    if conf < s.book_review_confidence:
        why = ev.get("score_reason") or "low evidence"
        if dst and cls["kind"] == "ebook" and ev["title"] and not title_is_garbage(ev["title"]):
            # a clearly titled book is never silently rejected: a human looks at it
            conf = s.book_review_confidence
            rationale = f"review: {why} (clear title from {lk['title_source'] or 'llm'})"
        else:
            rationale = f"reject: {why}" + ("" if dst else "; no destination (no title/kind)")
    return Suggestion(domain="book", source_path=str(path), target_path=str(dst) if dst else None,
                      metadata={"embedded": meta, "reference": ref, "classification": cls},
                      confidence=round(conf, 3), decision=decide(conf, s),
                      rationale=rationale, media_kind=cls["kind"],
                      op="move" if dst else "none", run_id=run_id)


def iter_book_files(dirs: list[Path]):
    exts = BOOK_EXTENSIONS | AUDIO_EXTENSIONS
    for d in dirs:
        if not d.is_dir():
            continue
        for p in sorted(d.rglob("*")):
            if p.is_file() and not p.name.startswith(".") and p.suffix.lower() in exts:
                yield p
