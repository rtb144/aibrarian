"""Book metadata cleanup and an evidence-based confidence score.

Score (0-1), from the strongest evidence available (see docs/operations.md):

====================================================  =========================================
evidence                                              score
====================================================  =========================================
ISBN lookup hit (Open Library / Google) and its       0.97
title agrees with the embedded title (>= 80 %)
ISBN lookup hit, titles disagree                      0.85
embedded title + author, reference found              0.50 + 0.45 * min(title%, author%)
                                                      (both 100 % -> 0.95, both 80 % -> 0.86)
embedded title + author, no reference                 0.70
embedded title only, reference found                  0.40 + 0.30 * title%   (<= 0.70)
title only from the file name / LLM                   0.35 (+0.20 * title% with a reference)
title is junk (digits / ISBN-like file name)          0.10
no author at all                                      capped at 0.30
====================================================  =========================================

Only the first two rows and near-perfect title+author agreement reach the default 0.90
auto-apply threshold.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from rapidfuzz import fuzz

_NUM = r"(\d+(?:\.\d+)?)"
_IDX = rf"(?:book|bk\.?|vol\.?|volume|no\.?|part|#)\s*{_NUM}"
_PAREN_SERIES = re.compile(rf"\s*[\(\[]\s*(?P<series>[^()\[\]]*?)[\s,:;-]*{_IDX}\s*[\)\]]\s*$", re.I)
_IDX_ONLY = re.compile(rf"^\s*{_IDX}\s*$", re.I)
_IDX_IN = re.compile(rf"^(?P<series>.*?)[\s,:;-]*{_IDX}\s*$", re.I)
_GENRE = re.compile(r"^(?:a|an)\s+.+|.*\b(?:novel|novella|mystery|mysteries|thriller|romance|"
                    r"story|stories|memoir|guide|handbook|edition|collection|series|saga|"
                    r"trilogy|cozy|fantasy|adventure|biography)\b.*", re.I)
_JUNK_TITLE = re.compile(r"^[\d\s_\-xX.]+$")


def _tidy(s: str) -> str:
    s = re.sub(r"\s+:\s+|\s+:|:\s*", ": ", s)          # ' : ' -> ': '
    s = re.sub(r"\s+", " ", s).strip(" -:;,_.")
    if s.isupper() and len(s) > 3:
        s = s.title()
    return s


def clean_title(raw: str | None, series: str | None = None,
                series_index: str | float | None = None) -> dict[str, Any]:
    """Split series/index and genre subtitles out of a messy title.

    'AMNESIA - The Josephine Chronicles : Book 1 - FBI Series' ->
        title 'Amnesia', series 'The Josephine Chronicles', index 1, subtitle 'FBI Series'
    'Dark Fortune: A Cozy Mystery (Fortunes, Love Book 6)' ->
        title 'Dark Fortune', series 'Fortunes, Love', index 6, subtitle 'A Cozy Mystery'
    """
    out = {"title": None, "subtitle": None, "series": series or None,
           "series_index": float(series_index) if series_index not in (None, "") else None}
    if not raw:
        return out
    t = _tidy(str(raw))
    m = _PAREN_SERIES.search(t)
    if m:
        out["series"] = out["series"] or (_tidy(m.group("series")) or None)
        out["series_index"] = out["series_index"] or float(m.group(2))
        t = t[:m.start()]
    parts = [p for p in (_tidy(x) for x in re.split(r"\s+-\s+|:\s+", t)) if p]
    if not parts:
        return out
    title, rest = parts[0], parts[1:]
    subtitle = []
    for i, p in enumerate(rest):
        if _IDX_ONLY.match(p):                                 # '... : Book 1'
            out["series_index"] = out["series_index"] or float(_IDX_ONLY.match(p).group(1))
            if not out["series"] and i > 0 and rest[i - 1] in subtitle:
                out["series"] = rest[i - 1]
                subtitle.remove(rest[i - 1])
            continue
        mi = _IDX_IN.match(p)
        if mi and mi.group("series"):                          # 'Fortunes Book 6'
            out["series"] = out["series"] or _tidy(mi.group("series"))
            out["series_index"] = out["series_index"] or float(mi.group(2))
            continue
        subtitle.append(p)
    keep = []  # non-genre subtitles stay part of the title ('Star Wars: Thrawn')
    for p in subtitle:
        if p == out["series"]:
            continue
        if _GENRE.match(p) or out["series_index"] is not None:
            out["subtitle"] = f"{out['subtitle']}; {p}" if out["subtitle"] else p
        else:
            keep.append(p)
    out["title"] = ": ".join([title] + keep)
    if out["series_index"] is not None and out["series_index"].is_integer():
        out["series_index"] = int(out["series_index"])
    return out


def _ratio(a: str | None, b: str | None) -> float:
    if not a or not b:
        return 0.0
    from .openlibrary import norm  # punctuation-insensitive: 'Leckie, Ann' == 'Ann Leckie'

    return fuzz.token_set_ratio(norm(str(a)), norm(str(b))) / 100


def _author_ratio(a: str | None, b: str | None) -> float:
    """Author agreement, accent/case/punctuation-insensitive: same surname and same first
    initial = 1.0 ('Lawrence M. Krauss' ~ 'Lawrence Maxwell Krauss', 'Ben Dupre' ~
    'Ben Dupré', 'Warburton, Nigel' ~ 'Nigel Warburton'); multi-author strings match on
    any pair; else fuzzy token-set ratio."""
    if not a or not b:
        return 0.0
    from .olindex import surname
    from .openlibrary import norm

    def first_initial(n: str) -> str:
        n = n.split(";")[0]
        if "," in n:
            n = n.split(",", 1)[1]
        toks = norm(n).split()
        return toks[0][:1] if toks else ""

    best = _ratio(a, b)
    for x in str(a).split(";"):
        for y in str(b).split(";"):
            sx, sy = surname(x), surname(y)
            if sx and sx == sy:
                fx, fy = first_initial(x), first_initial(y)
                best = max(best, 1.0 if (fx == fy or not fx or not fy) else 0.75)
    return best


def is_junk_title(t: str | None) -> bool:
    return not t or bool(_JUNK_TITLE.match(str(t).strip()))


def score(embedded: dict[str, Any], ref: dict[str, Any] | None,
          title: str | None, author: str | None) -> tuple[float, str]:
    """``(confidence, why)`` from the evidence; ``title``/``author`` are the final values."""
    from .contentscan import title_is_garbage

    emb_t, emb_a = embedded.get("title"), embedded.get("author")
    if is_junk_title(emb_t) or title_is_garbage(emb_t, embedded.get("filename")):
        emb_t = None
    src = "embedded"
    if not emb_t and embedded.get("name_title"):  # 'Title - Author.mobi'
        emb_t, src = embedded["name_title"], "file name"
        emb_a = emb_a or embedded.get("name_author")
    elif not emb_a and embedded.get("name_author"):
        emb_a = embedded["name_author"]
    ref = ref or {}
    isbn_hit = ref.get("match_type") == "isbn" or (
        embedded.get("isbn") and embedded.get("isbn") in (ref.get("isbn13"), ref.get("isbn10"),
                                                          ref.get("isbn")))
    if isbn_hit:
        t = _ratio(emb_t, ref.get("title")) if emb_t else 1.0
        s, why = (0.97, "ISBN match, title agrees") if t >= 0.8 else \
            (0.85, f"ISBN match, title differs ({t:.0%})")
    elif emb_t and emb_a and ref:
        t = max(_ratio(emb_t, ref.get("title")), _ratio(clean_title(emb_t)["title"],
                                                         ref.get("title")))
        a = max([_author_ratio(emb_a, x) for x in (ref.get("authors") or [])]
                + [_author_ratio(emb_a, ref.get("author"))])
        s, why = 0.50 + 0.45 * min(t, a), f"{src} title {t:.0%} / author {a:.0%} vs reference"
    elif emb_t and emb_a:
        s, why = (0.70 if src == "embedded" else 0.65), f"{src} title+author, no reference"
    elif emb_t and ref:
        t = _ratio(emb_t, ref.get("title"))
        s, why = 0.40 + 0.30 * t, f"{src} title only ({t:.0%} vs reference)"
    elif is_junk_title(title):
        s, why = 0.10, "no usable title (junk file name)"
    else:
        t = _ratio(title, ref.get("title")) if ref else 0.0
        s, why = 0.35 + 0.30 * t, "title from file name / LLM only" + (
            f" ({t:.0%} vs reference)" if ref else "")
    if not author or author.strip().lower() in ("unknown", "unknown author"):
        s, why = min(s, 0.30), why + "; no author"
    return round(s, 3), why


def evaluate(sug: dict[str, Any], ref_lookup=None) -> dict[str, Any]:
    """Clean metadata + score for a stored book suggestion (old ones included).

    ``ref_lookup(title, author, isbns)`` (the local Open Library index, see
    :mod:`aibrarian.olindex`) re-queries the reference when given: ISBNs come from the
    embedded metadata, a sidecar/``metadata.opf`` and check-digit-valid digits in the file
    name (``1503071278.epub``)."""
    md = sug.get("metadata") or {}
    emb = md.get("embedded") or {}
    ref = md.get("reference") or {}
    cls = md.get("classification") or {}
    src = Path(sug["source_path"])
    from .contentscan import title_is_garbage

    raw_title = emb.get("title")
    if is_junk_title(raw_title) or title_is_garbage(raw_title, emb.get("filename")):
        raw_title = None
    if ref_lookup is not None:
        from .olindex import isbn13, isbns_in_name, opf_isbns

        isbns = [i for i in [isbn13(emb.get("isbn"))] + opf_isbns(src) + isbns_in_name(src.name)
                 + isbns_in_name(str(emb.get("title") or ""))
                 + [isbn13(x) for x in emb.get("content_isbns") or []] if i]
        q_title = clean_title(raw_title or emb.get("name_title") or cls.get("title"))["title"]
        try:
            hit = ref_lookup(q_title, emb.get("author") or emb.get("name_author")
                             or cls.get("author"),
                             list(dict.fromkeys(isbns)))
        except Exception:
            hit = None
        if hit:
            ref = hit
            if hit.get("match_type") == "isbn" and not emb.get("isbn"):
                emb = emb | {"isbn": hit.get("isbn13")}
    raw_title = raw_title or ref.get("title") or emb.get("name_title") or cls.get("title")
    if is_junk_title(raw_title):
        raw_title = None
    author = emb.get("author") or emb.get("name_author") or ref.get("author") or cls.get("author")
    c = clean_title(raw_title, emb.get("series") or cls.get("series"), emb.get("series_index"))
    conf, why = score(emb, ref or None, c["title"], author)
    if ref.get("source") == "openlibrary-index":
        why += f" [Open Library {ref.get('edition_key') or ref.get('work_key')}]"
        # canonical author spelling from Open Library when the match is strong
        if ref.get("author") and conf >= 0.85 and (
                not author or _author_ratio(author, ref["author"]) >= 0.8
                or ref.get("match_type") == "isbn"):
            author = ref["author"]
        if not c["subtitle"] and ref.get("subtitle") and _GENRE.match(ref["subtitle"]):
            c["subtitle"] = ref["subtitle"]
    isbn = emb.get("isbn") or ref.get("isbn13") or ref.get("isbn10")
    ids = {}
    if ref.get("source") == "openlibrary-index":
        ids = {k: ref[k] for k in ("edition_key", "work_key") if ref.get(k)}
    return {"title": c["title"], "subtitle": c["subtitle"], "author": author,
            "series": c["series"], "series_index": c["series_index"], "isbn": isbn,
            "kind": sug.get("media_kind") or cls.get("kind"), "confidence": conf,
            "score_reason": why, "stored_confidence": sug.get("confidence"),
            "openlibrary": ids or None, "source_name": src.name}
