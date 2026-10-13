"""Cheap content scan of a book's own text (ISBN / title / author).

Used by ``books.extract_metadata`` only when the embedded metadata is weak (no title, a
garbage/filename-like title, no author or no ISBN). Reads at most the first
``FRONT_PAGES`` pages plus the last ``BACK_PAGES`` (PDF via PyMuPDF) or the first and
last few spine documents (EPUB, stdlib zipfile), capped by ``MAX_CHARS`` and a wall-clock
budget (``AIB_BOOK_CONTENT_SCAN_S``, default 15s). mobi/azw3/cbr/cbz are skipped.
"""

from __future__ import annotations

import html
import posixpath
import re
import time
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Any

FRONT_PAGES = 10
BACK_PAGES = 2
MAX_CHARS = 200_000
PAGE_CHARS = 20_000
SNIPPET_CHARS = 1500
EPUB_FRONT_DOCS = 6
EPUB_BACK_DOCS = 2
EPUB_DOC_BYTES = 400_000

_GARBAGE = re.compile(
    r"^(microsoft (word|powerpoint|excel)\b|untitled\b|unknown$|no title$|document\s*\d*$|doc\d+$"
    r"|scan(ned)?(\s*\d.*|\s*(document|image|file|page)s?\b.*)?$|scan_|img[_ -]?\d|image\d|dsc[_ ]?\d"
    r"|adobe (acrobat|photoshop|indesign)|pdf ?document$|title$|book$|e-?book$|cover$|layout \d"
    r"|(hp |hewlett.packard )?(scanjet|officejet|laserjet)\b"
    r"|(canon|epson|xerox|ricoh|brother|kofax|fujitsu|konica( minolta)?|sharp|paperport|scansnap)"
    r"((\s+|[-_])(?=\S*[\d-])[\w-]+|\s+[A-Z]\w+(?=\s+\S*\d))*(\s+(scan|mfp|document))?\s*$|tmp|temp$)",
    re.I)
_FILE_EXT = re.compile(r"\.(docx?|pdf|rtf|txt|indd|qxd|qxp|tex|dvi|ps|html?|odt|pages|wpd|p65)\s*$",
                       re.I)


def title_is_garbage(title: str | None, filename: str | None = None) -> bool:
    t = (title or "").strip()
    if len(t) < 2 or not re.search(r"[^\W\d_]", t):
        return True  # empty, all digits / punctuation
    if _GARBAGE.search(t) or _FILE_EXT.search(t):
        return True
    if re.fullmatch(r"[\w-]*\d{4,}[\w-]*", t) and " " not in t:
        return True  # e.g. 20190311_123456, abc12345
    if filename:
        stem = Path(filename).stem
        if t.lower() == stem.lower() and ("_" in t or " " not in t) and len(t) > 12:
            return True  # title is just the (machine-ish) filename
    return False


def title_is_garbled(title: str | None) -> bool:
    """Text-layer garbage from a title page, e.g. small-caps glyphs that lost their first
    letters ('EITEL OW TO ROGRAM ERIES') or kerning fragments ('usy Co de id to ro lo')."""
    t = (title or "").strip()
    words = re.findall(r"[^\W\d_]+", t)
    if not words:
        return True
    letters = "".join(words)
    vowels = sum(c in "aeiouyAEIOUY" for c in letters) / len(letters)
    if len(letters) >= 6 and not 0.2 <= vowels <= 0.7:
        return True
    short = sum(len(w) <= 2 for w in words)
    if len(words) >= 3 and short / len(words) >= 0.4:
        return True
    if len(words) >= 3 and sum(len(w) for w in words) / len(words) < 3.2:
        return True
    lower_start = sum(w[0].islower() for w in words)
    camel = bool(re.match(r"[a-z]+[A-Z]", t))  # iOS, eBay, iPhone
    if len(words) >= 3 and lower_start / len(words) > 0.6 and not t[0].isupper() and not camel:
        return True  # starts mid-word ('usy Co ...')
    return (t[0].islower() and len(words) >= 2 and not camel
            and not re.match(r"(a|an|the|on|of|in|for|to|and|e)\b", t))


def weak_reasons(meta: dict[str, Any]) -> list[str]:
    """Why embedded metadata is weak (empty list = strong enough, skip the scan)."""
    out = []
    if not meta.get("title"):
        out.append("no_title")
    elif title_is_garbage(meta.get("title"), meta.get("filename")):
        out.append("garbage_title")
    if not meta.get("author") or title_is_garbage(meta.get("author")):
        out.append("no_author")
    if not meta.get("isbn"):
        out.append("no_isbn")
    return out


# -- ISBN ---------------------------------------------------------------------
def isbn10_ok(d: str) -> bool:
    if not re.fullmatch(r"\d{9}[\dX]", d):
        return False
    s = sum((10 - i) * (10 if c == "X" else int(c)) for i, c in enumerate(d))
    return s % 11 == 0


def isbn13_ok(d: str) -> bool:
    if not re.fullmatch(r"97[89]\d{10}", d):
        return False
    s = sum(int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(d[:12]))
    return (10 - s % 10) % 10 == int(d[12])


_ISBN_CAND = re.compile(r"(?<![\dX])(\d[\d\- \u2010-\u2015]{8,16}[\dXx])(?![\d])")
_COPYRIGHT = re.compile(r"copyright|\u00a9|all rights reserved|first published|printed in", re.I)
_OTHER_FMT = re.compile(r"\b(audio ?book|audio cd|cd-rom|dvd|large print)\b", re.I)


def find_isbns(pages: list[str]) -> list[dict[str, Any]]:
    """Checksum-valid ISBN-10/13 candidates, best first. ISBN-10s and bare numbers need
    an 'ISBN' label nearby; candidates labelled as audio/CD/DVD/large print rank last."""
    found: dict[str, dict[str, Any]] = {}
    for pno, text in enumerate(pages):
        if not text:
            continue
        cpage = bool(_COPYRIGHT.search(text))
        for m in _ISBN_CAND.finditer(text):
            d = re.sub(r"[^\dXx]", "", m.group(1)).upper()
            before = text[max(0, m.start() - 40):m.start()]
            labelled = bool(re.search(r"isbn", before, re.I))
            if len(d) == 13 and isbn13_ok(d) or len(d) == 10 and isbn10_ok(d) and labelled:
                pass
            else:
                continue
            ls = text.rfind("\n", 0, m.start()) + 1
            le = text.find("\n", m.end())
            ctx = text[max(ls, m.start() - 40):(le if le >= 0 else len(text))][:120]
            score = 3 * labelled + 2 * cpage + (len(d) == 13) - 10 * bool(_OTHER_FMT.search(ctx))
            prev = found.get(d)
            if prev is None or score > prev["score"]:
                found[d] = {"isbn": d, "score": score, "page": pno}
    return sorted(found.values(), key=lambda c: (-c["score"], c["page"]))


# -- title / author guesses ---------------------------------------------------
_BY = re.compile(r"(?:^|\n|\s)[Bb]y[:\s]+([A-Z][\w.'\u2019-]+(?:[ \t]+(?:[A-Z][\w.'\u2019-]*|de|van|von|"
                 r"der|la|le)){1,4})")


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip(" \t\n-:|,;")


def looks_like_name(s: str) -> bool:
    w = s.split()
    return 2 <= len(w) <= 5 and all(x[0].isupper() or x in ("de", "van", "von", "der", "la", "le")
                                    for x in w) and not re.search(r"\d", s) and len(s) <= 60


def author_from_text(pages: list[str]) -> str | None:
    for text in pages[:4]:
        for m in _BY.finditer(text or ""):
            name = _clean(m.group(1))
            if looks_like_name(name):
                return name
    return None


def title_from_spans(spans: list[tuple[float, str]]) -> tuple[str | None, str | None]:
    """(title, author guess) from (font size, text) spans of the first pages: the title is
    the text in the largest font; a name-like line in the next size down is the author."""
    spans = [(round(sz, 1), _clean(t)) for sz, t in spans if _clean(t)]
    spans = [(sz, t) for sz, t in spans if re.search(r"[^\W\d_]{2}", t)]
    if not spans:
        return None, None
    sizes = sorted({sz for sz, _ in spans}, reverse=True)
    title = None
    for sz in sizes[:3]:
        cand = _clean(" ".join(t for s, t in spans if s == sz))[:200]
        if (2 <= len(cand) <= 150 and not title_is_garbage(cand)
                and not title_is_garbled(cand)):
            title = cand
            break
    author = None
    for sz in sizes[:5]:
        for s, t in spans:
            if s == sz and t != title and looks_like_name(re.sub(r"^[Bb]y\s+", "", t)):
                author = re.sub(r"^[Bb]y\s+", "", t)
                break
        if author:
            break
    return title, author


# -- readers --------------------------------------------------------------------
def pdf_pages(path: Path, deadline: float) -> tuple[list[str], list[tuple[float, str]]]:
    from .books import mupdf

    fitz = mupdf()
    pages: list[str] = []
    spans: list[tuple[float, str]] = []
    total = 0
    with fitz.open(str(path)) as doc:
        n = doc.page_count
        idx = list(range(min(FRONT_PAGES, n)))
        idx += [i for i in range(max(0, n - BACK_PAGES), n) if i not in idx]
        for i in idx:
            if time.monotonic() > deadline or total > MAX_CHARS:
                break
            page = doc.load_page(i)
            t = page.get_text("text")[:PAGE_CHARS]
            pages.append(t)
            total += len(t)
            if i < 3:
                for b in page.get_text("dict").get("blocks", []):
                    for line in b.get("lines", []):
                        for sp in line.get("spans", []):
                            spans.append((float(sp.get("size") or 0), sp.get("text") or ""))
    return pages, spans


_TAG = re.compile(r"<[^>]+>")


def _strip_html(raw: str) -> str:
    raw = re.sub(r"(?is)<(script|style|head)\b.*?</\1>", " ", raw)
    raw = re.sub(r"(?i)<(br|/p|/div|/h\d|/li|/tr)[^>]*>", "\n", raw)
    return re.sub(r"[ \t\r\f\v]+", " ", html.unescape(_TAG.sub(" ", raw)))


def epub_pages(path: Path, deadline: float) -> tuple[list[str], list[tuple[float, str]]]:
    pages: list[str] = []
    spans: list[tuple[float, str]] = []
    with zipfile.ZipFile(path) as z:
        container = ET.fromstring(z.read("META-INF/container.xml"))
        opf_path = next(e for e in container.iter() if e.tag.endswith("rootfile")).attrib["full-path"]
        opf = ET.fromstring(z.read(opf_path))
        base = posixpath.dirname(opf_path)
        manifest = {e.attrib.get("id"): e.attrib.get("href") for e in opf.iter()
                    if e.tag.endswith("}item") or e.tag == "item"}
        spine = [manifest.get(e.attrib.get("idref")) for e in opf.iter()
                 if e.tag.endswith("itemref") or e.tag == "itemref"]
        spine = [h for h in spine if h]
        picks = spine[:EPUB_FRONT_DOCS] + [h for h in spine[-EPUB_BACK_DOCS:]
                                           if h not in spine[:EPUB_FRONT_DOCS]]
        total = 0
        for i, href in enumerate(picks):
            if time.monotonic() > deadline or total > MAX_CHARS:
                break
            name = posixpath.normpath(posixpath.join(base, href.split("#")[0]))
            try:
                with z.open(name) as fh:
                    raw = fh.read(EPUB_DOC_BYTES).decode("utf-8", "replace")
            except KeyError:
                continue
            if i < 3:  # heading levels stand in for font size
                for lvl, txt in re.findall(r"(?is)<h([1-3])\b[^>]*>(.*?)</h\1>", raw):
                    spans.append((float(40 - 10 * int(lvl)), _strip_html(txt)))
                for txt in re.findall(r'(?is)<[^>]+class="[^"]*(?:title|author)[^"]*"[^>]*>(.*?)</',
                                      raw):
                    spans.append((15.0, _strip_html(txt)))
            t = _strip_html(raw)[:PAGE_CHARS]
            pages.append(t)
            total += len(t)
    return pages, spans


def _snippet(pages: list[str]) -> str:
    """Title + copyright pages first (that is where the identifying text is)."""
    pick = [p for p in pages[:3]] + [p for p in pages[3:] if _COPYRIGHT.search(p or "")][:1]
    return _clean(" \n".join(pick))[:SNIPPET_CHARS]


def scan(path: Path, budget_s: float = 15.0) -> dict[str, Any]:
    """Return content_* keys (content_scan=True always, plus content_error on failure)."""
    out: dict[str, Any] = {"content_scan": True}
    ext = path.suffix.lower()
    deadline = time.monotonic() + budget_s
    t0 = time.monotonic()
    try:
        if ext == ".pdf":
            pages, spans = pdf_pages(path, deadline)
        elif ext == ".epub":
            pages, spans = epub_pages(path, deadline)
        else:
            out["content_skipped"] = ext or "no extension"
            return out
    except Exception as exc:
        out["content_error"] = f"{type(exc).__name__}: {exc}"[:300]
        return out
    isbns = find_isbns(pages)
    title, author = title_from_spans(spans)
    author = author_from_text(pages) or author
    out |= {"content_isbn": isbns[0]["isbn"] if isbns else None,
            "content_isbns": [c["isbn"] for c in isbns[:5]] or None,
            "content_title": title, "content_author": author,
            "content_snippet": _snippet(pages) or None,
            "content_scan_s": round(time.monotonic() - t0, 2)}
    return {k: v for k, v in out.items() if v not in (None, "", [])}
