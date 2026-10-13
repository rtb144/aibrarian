"""Read-only transcript language check.

Finds ``<video>.transcript.json`` sidecars under the transcribe roots (plus DB
transcripts whose sidecar is missing, sampled from ``aib.segment``), detects
the language of a text sample with ``langdetect`` (pure Python, offline) and
flags transcripts that are not English, whose detected language disagrees with
the language whisper stored, or whose text looks like a hallucination
(repeated lines, almost no text).  Writes JSON + CSV reports to ``report_dir``
and (optionally) upserts the rows into ``aib.transcript_lang`` for review.

Never writes, moves or renames media or transcript files.
"""

from __future__ import annotations

import csv
import json
import os
import re
from collections import Counter
from collections.abc import Callable, Iterable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SUFFIX = ".transcript.json"
SAMPLE_CHARS = 4000
SNIPPET_CHARS = 200
REPEAT_RATIO_FLAG = 0.5     # share of segments that duplicate an earlier one
TOP_LINE_SHARE_FLAG = 0.3   # share of segments that are the single most common line
MIN_SEGMENTS_FOR_REPEAT = 10
MIN_LETTERS = 40            # less than this in the whole transcript = near-empty
LOW_CONFIDENCE = 0.80
REPORT_FIELDS = ["path", "media_id", "title", "source", "detected_lang", "confidence",
                 "stored_lang", "mismatch", "non_english", "repetitive", "repeat_ratio",
                 "top_line_share", "segments", "chars", "flags", "snippet"]


# --- text helpers -----------------------------------------------------------

def _norm_line(t: str) -> str:
    return re.sub(r"[\W_]+", " ", t.lower()).strip()


def norm_lang(code: str | None) -> str | None:
    """'en', 'EN', 'english', 'zh-cn' -> 'en'/'en'/'en'/'zh'."""
    if not code:
        return None
    c = str(code).strip().lower().replace("_", "-")
    names = {"english": "en", "german": "de", "french": "fr", "spanish": "es", "italian": "it",
             "japanese": "ja", "chinese": "zh", "korean": "ko", "russian": "ru",
             "portuguese": "pt", "dutch": "nl", "swedish": "sv"}
    c = names.get(c, c)
    return c.split("-")[0] or None


def repetition_stats(lines: list[str]) -> tuple[float, float]:
    """(repeat_ratio, top_line_share) over non-empty normalized lines."""
    norm = [n for n in (_norm_line(x) for x in lines) if n]
    if not norm:
        return 0.0, 0.0
    cnt = Counter(norm)
    return 1 - len(cnt) / len(norm), cnt.most_common(1)[0][1] / len(norm)


def sample_text(lines: list[str], max_chars: int = SAMPLE_CHARS) -> str:
    """Evenly spread sample of distinct lines (so intros/credits don't dominate)."""
    uniq: list[str] = []
    seen: set[str] = set()
    for x in lines:
        n = _norm_line(x)
        if n and n not in seen:
            seen.add(n)
            uniq.append(x.strip())
    if not uniq:
        return ""
    total = sum(len(x) + 1 for x in uniq)
    if total <= max_chars:
        return " ".join(uniq)
    step = total / max_chars
    picked, acc, i = [], 0, 0.0
    while int(i) < len(uniq) and acc < max_chars:
        picked.append(uniq[int(i)])
        acc += len(uniq[int(i)]) + 1
        i += step
    return " ".join(picked)


def detect(text: str) -> tuple[str | None, float]:
    """(language, probability) via langdetect; deterministic (seeded)."""
    letters = sum(ch.isalpha() for ch in text)
    if letters < 20:
        return None, 0.0
    from langdetect import DetectorFactory, detect_langs
    from langdetect.lang_detect_exception import LangDetectException

    DetectorFactory.seed = 0
    try:
        best = detect_langs(text)[0]
    except LangDetectException:
        return None, 0.0
    return norm_lang(best.lang), round(float(best.prob), 4)


def analyze(lines: list[str], stored_lang: str | None) -> dict[str, Any]:
    text = " ".join(x.strip() for x in lines if x and x.strip())
    lang, conf = detect(sample_text(lines))
    rr, top = repetition_stats(lines)
    stored = norm_lang(stored_lang)
    letters = sum(ch.isalpha() for ch in text)
    flags: list[str] = []
    non_en = lang is not None and lang != "en"
    if non_en:
        flags.append("non_english")
    mismatch = bool(stored and lang and stored != lang)
    if mismatch:
        flags.append("lang_mismatch")
    if stored and stored != "en" and not non_en:
        flags.append("stored_non_english")
    repetitive = len(lines) >= MIN_SEGMENTS_FOR_REPEAT and (
        rr >= REPEAT_RATIO_FLAG or top >= TOP_LINE_SHARE_FLAG)
    if repetitive:
        flags.append("repetitive")
    if letters < MIN_LETTERS:
        flags.append("near_empty")
    if lang is None:
        flags.append("undetected")
    elif conf < LOW_CONFIDENCE:
        flags.append("low_confidence")
    snippet = sample_text(lines, SNIPPET_CHARS * 2)[:SNIPPET_CHARS]
    return {"detected_lang": lang, "confidence": conf, "stored_lang": stored,
            "mismatch": mismatch, "non_english": non_en, "repetitive": repetitive,
            "repeat_ratio": round(rr, 4), "top_line_share": round(top, 4),
            "segments": len(lines), "chars": len(text), "flags": flags, "snippet": snippet}


def needs_review(row: dict[str, Any]) -> bool:
    return bool(row.get("flags")) and row["flags"] != ["low_confidence"]


# --- discovery --------------------------------------------------------------

def iter_sidecars(roots: Iterable[Path]) -> Iterator[Path]:
    for root in roots:
        if not root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith((".", "_quarantine")))
            for f in sorted(filenames):
                if f.endswith(SUFFIX) and not f.startswith("."):
                    yield Path(dirpath) / f


def read_sidecar(p: Path) -> tuple[list[str], str | None]:
    data = json.loads(p.read_text(encoding="utf-8"))
    segs = data.get("segments") if isinstance(data, dict) else data
    lines = [str(s.get("text", "")) for s in segs or [] if isinstance(s, dict)]
    lang = data.get("language") if isinstance(data, dict) else None
    return lines, lang


def _title_from_path(p: Path) -> str:
    return p.name[: -len(SUFFIX)] if p.name.endswith(SUFFIX) else p.stem


# --- run --------------------------------------------------------------------

def run(ctx, limit: int | None = None, out: Path | None = None, store: bool = True,
        include_db: bool = True, progress: Callable[[str], None] | None = None) -> dict[str, Any]:
    """Check every transcript, write ``langcheck.json`` + ``langcheck.csv``."""
    s = ctx.settings
    say = progress or (lambda _m: None)
    db = ctx.db
    media_by_sidecar: dict[str, dict[str, Any]] = {}
    db_only: list[dict[str, Any]] = []
    try:
        for m in db.transcribed_media_for_langcheck():
            tp = str(Path(m["path"]).with_suffix(SUFFIX))
            media_by_sidecar[tp] = m
    except Exception as exc:  # DB down -> filesystem-only report
        say(f"DB unavailable, titles from file names only: {exc}")
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    errors = 0
    for p in iter_sidecars(s.transcribe_paths()):
        if limit is not None and len(rows) >= limit:
            break
        seen.add(str(p))
        m = media_by_sidecar.get(str(p), {})
        base = {"path": str(p), "media_id": m.get("id"),
                "title": m.get("title") or _title_from_path(p), "source": "sidecar"}
        try:
            lines, stored = read_sidecar(p)
        except (OSError, ValueError) as exc:
            errors += 1
            rows.append(base | {"flags": ["unreadable"], "snippet": str(exc)[:SNIPPET_CHARS]})
            continue
        rows.append(base | analyze(lines, stored or m.get("language")))
        if len(rows) % 200 == 0:
            say(f"checked {len(rows)} transcripts")
    if include_db:
        db_only = [m for tp, m in media_by_sidecar.items() if tp not in seen]
        for m in db_only:
            if limit is not None and len(rows) >= limit:
                break
            try:
                lines = [r["text"] for r in db.segments_for_media(m["id"])]
            except Exception:
                continue
            rows.append({"path": str(Path(m["path"]).with_suffix(SUFFIX)), "media_id": m["id"],
                         "title": m.get("title"), "source": "db"}
                        | analyze(lines, m.get("language")))
    for r in rows:
        for k in REPORT_FIELDS:
            r.setdefault(k, None)
        r["needs_review"] = needs_review(r)
    summary = summarize(rows) | {"errors": errors}
    out_dir = Path(out) if out else Path(s.report_dir)
    paths = write_reports(rows, summary, out_dir)
    stored = 0
    if store:
        try:
            stored = db.save_langcheck(rows)
        except Exception as exc:
            say(f"could not store results in aib.transcript_lang: {exc}")
    return {"summary": summary | {"stored": stored, **paths}, "rows": rows}


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    langs = Counter(r.get("detected_lang") or "?" for r in rows)
    flags = Counter(f for r in rows for f in (r.get("flags") or []))
    return {"checked": len(rows), "needs_review": sum(bool(r.get("needs_review")) for r in rows),
            "non_english": flags.get("non_english", 0), "mismatch": flags.get("lang_mismatch", 0),
            "repetitive": flags.get("repetitive", 0), "languages": dict(langs.most_common()),
            "flags": dict(flags.most_common()),
            "generated_at": datetime.now(UTC).isoformat(timespec="seconds")}


def write_reports(rows: list[dict[str, Any]], summary: dict[str, Any], out: Path) -> dict[str, str]:
    """``out`` is a directory, or a file path whose stem names both reports."""
    if out.suffix.lower() in (".json", ".csv"):
        jp, cp = out.with_suffix(".json"), out.with_suffix(".csv")
    else:
        jp, cp = out / "langcheck.json", out / "langcheck.csv"
    jp.parent.mkdir(parents=True, exist_ok=True)
    tmp = jp.with_name(f".{jp.name}.tmp")
    tmp.write_text(json.dumps({"summary": summary, "items": rows}, ensure_ascii=False,
                              indent=1, default=str), encoding="utf-8")
    os.replace(tmp, jp)
    ordered = sorted(rows, key=lambda r: (not r.get("needs_review"), r.get("path") or ""))
    tmp = cp.with_name(f".{cp.name}.tmp")
    with tmp.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=[*REPORT_FIELDS, "needs_review"], extrasaction="ignore")
        w.writeheader()
        for r in ordered:
            w.writerow(r | {"flags": ";".join(r.get("flags") or [])})
    os.replace(tmp, cp)
    return {"json": str(jp), "csv": str(cp)}


def format_summary(res: dict[str, Any], top: int = 25) -> str:
    s = res["summary"]
    lines = [f"checked {s['checked']} transcripts: {s['needs_review']} need review "
             f"(non-English {s['non_english']}, mismatch {s['mismatch']}, "
             f"repetitive {s['repetitive']}, errors {s['errors']})",
             "languages: " + ", ".join(f"{k}={v}" for k, v in list(s["languages"].items())[:12]),
             f"report: {s['json']}", f"        {s['csv']}"]
    flagged = [r for r in res["rows"] if r.get("needs_review")]
    for r in flagged[:top]:
        lines.append(f"  [{r.get('detected_lang') or '?'} {r.get('confidence') or 0:.2f}"
                     f" stored={r.get('stored_lang') or '-'}] {','.join(r['flags'])}  "
                     f"{r.get('title')}  ({r['path']})")
    if len(flagged) > top:
        lines.append(f"  ... {len(flagged) - top} more in the report")
    return "\n".join(lines) + "\n"
