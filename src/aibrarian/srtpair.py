"""Pair orphan ``.srt`` files with the single video in their movie folder (``srt-pair``).

For every movie dir (direct child of ``--root``) holding subtitles that no video claims:

* exactly one video (``sample`` files ignored): each kept-language sub is renamed to the
  Jellyfin form ``<video stem>.<lang>[.sdh|.forced][.N].srt``. Language comes from the
  filename (``eng``/``en``/``English`` ...), else from the text via langdetect. Existing
  paired subs are never overwritten: an identical one sends the orphan to quarantine as a
  dup, a different one gets the next free suffix (``.1``, ``.2``; more go to quarantine);
* subs in languages not in ``--langs`` (default ``eng`` / env ``AIB_SUB_LANGS``) go to
  quarantine;
* no video: reported as ``no_video`` (with a sibling-dir hint); quarantined only with
  ``include_no_video``;
* several videos: ``ambiguous``, skipped.

Every move goes through :func:`aibrarian.changelog.change` (locks + change_log row).
Quarantined files land in ``QUARANTINE_DIR/<date>/...`` with records in that batch's
``manifest.jsonl``; renames are recorded in ``QUARANTINE_DIR/<date>/srt-pair/manifest.jsonl``
(path = old name, quarantine_path = new name), so both are undone with
``aibrarian duplicates restore --manifest <file> --execute``. Emptied ``Subs/`` dirs are
removed with ``rmdir`` only.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections import Counter
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from . import changelog
from .config import VIDEO_EXTENSIONS
from .quarantine import _qpath, _write_manifest, log_action

REASON = "srt_pair"
MAX_SUFFIX = 2  # .1 and .2 per language+flag; further different copies are quarantined
SUBS_DIRS = {"subs", "subtitles", "sub"}

# token -> ISO 639-2/B
_LANGS: dict[str, str] = {}
for code, names in {
    "eng": "en english", "spa": "es spanish espanol español esp lat latino",
    "fre": "fr fra french francais français", "ger": "de deu german deutsch",
    "ita": "it italian italiano", "por": "pt pob ptbr brazilian portuguese portugues",
    "rus": "ru russian", "jpn": "ja jp japanese", "chi": "zh zho chs cht chinese",
    "kor": "ko korean", "dut": "nl nld dutch", "swe": "sv swedish", "nor": "no nob nb norwegian",
    "dan": "da danish", "fin": "fi finnish", "pol": "pl polish", "tur": "tr turkish",
    "ara": "ar arabic", "heb": "he hebrew", "gre": "el ell greek", "hun": "hu hungarian",
    "cze": "cs ces czech", "rum": "ro ron romanian", "hin": "hi_lang hindi", "tha": "th thai",
    "vie": "vi vietnamese", "ind": "id indonesian", "may": "ms msa malay",
    "bul": "bg bulgarian", "hrv": "hr croatian", "srp": "sr serbian", "slv": "sl slovenian",
    "slo": "sk slk slovak", "ukr": "uk ukrainian", "est": "et estonian", "lav": "lv latvian",
    "lit": "lt lithuanian", "ice": "is isl icelandic", "per": "fa fas persian farsi",
    "cat": "ca catalan", "baq": "eu eus basque", "glg": "gl galician", "fil": "tl filipino",
    "tam": "ta tamil", "tel": "te telugu", "ben": "bn bengali", "mal": "ml malayalam",
}.items():
    _LANGS[code] = code
    for n in names.split():
        if n != "hi_lang":
            _LANGS[n] = code
# langdetect (2-letter) -> 639-2
_ISO1 = {k: v for k, v in _LANGS.items() if len(k) == 2}
_ISO1.update({"zh-cn": "chi", "zh-tw": "chi", "hi": "hin"})
# short codes that are common English words: only trusted as the last name token
_AMBIG = {"it", "no", "is", "id", "ms", "tl", "da", "de", "es", "et", "be", "to", "in",
          "ca", "eu", "gl", "lat", "sub", "ind", "may", "per", "cat", "est", "fil"}
SDH_TOKENS = {"sdh", "hi", "cc", "hoh"}
FORCED_TOKENS = {"forced", "foreign"}


def kept_langs(arg: str | None = None) -> list[str]:
    raw = arg or os.environ.get("AIB_SUB_LANGS") or "eng"
    out = []
    for t in re.split(r"[,\s]+", raw.strip()):
        if t:
            out.append(_LANGS.get(t.lower(), t.lower()))
    return list(dict.fromkeys(out))


def _tokens(name: str) -> list[str]:
    return [t for t in re.split(r"[.\s_\-\[\]()+,]+", name) if t]


def parse_hint(srt: Path, video_stem: str | None = None) -> dict[str, Any]:
    """Language / flags from the file name: last tokens of the stem (after removing the
    video stem prefix when the sub already starts with it)."""
    stem = srt.stem
    if video_stem and stem.startswith(video_stem):
        stem = stem[len(video_stem):]
    toks = _tokens(stem)
    in_subs = srt.parent.name.lower() in SUBS_DIRS
    lang = None
    sdh = forced = False
    tail = toks[-4:] if not in_subs else toks
    for i, t in enumerate(reversed(tail)):
        tl = t.lower()
        if tl.isdigit():
            continue
        if tl in SDH_TOKENS:
            sdh = True
            continue
        if tl in FORCED_TOKENS:
            forced = True
            continue
        if lang is None and tl in _LANGS:
            if tl in _AMBIG and not (i == 0 or in_subs and len(tl) > 2):
                continue
            lang = _LANGS[tl]
    return {"lang": lang, "sdh": sdh, "forced": forced}


_TS = re.compile(r"^\d+$|-->")
_TAG = re.compile(r"<[^>]+>|\{[^}]+\}")


def read_text(p: Path, limit: int = 64 * 1024) -> str:
    raw = p.read_bytes()[:limit]
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def detect_lang(p: Path) -> tuple[str | None, float]:
    from .langcheck import detect, sample_text

    lines = [_TAG.sub("", x).strip() for x in read_text(p).splitlines()]
    lines = [x for x in lines if x and not _TS.search(x)]
    lang, conf = detect(sample_text(lines))
    if not lang:
        return None, 0.0
    return _ISO1.get(lang, _LANGS.get(lang, lang)), conf


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def is_video(p: Path) -> bool:
    return p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS and \
        "sample" not in p.stem.lower()


def _norm_title(name: str) -> str:
    n = re.sub(r"\[[^\]]*\]", " ", name.lower())
    m = re.search(r"(19|20)\d\d", n)
    head = n[:m.end()] if m else n
    return re.sub(r"[^a-z0-9]+", " ", head).strip()


def _dir_srts(d: Path) -> list[Path]:
    out = [p for p in d.iterdir() if p.is_file() and p.suffix.lower() == ".srt"]
    for sd in d.iterdir():
        if sd.is_dir() and sd.name.lower() in SUBS_DIRS:
            out += [p for p in sd.rglob("*") if p.is_file() and p.suffix.lower() == ".srt"]
    return sorted(out)


def _paired(srt: Path, video: Path) -> bool:
    return srt.parent == video.parent and (srt.stem == video.stem or
                                           srt.name.startswith(video.stem + "."))


def slot_name(stem: str, lang: str, sdh: bool, forced: bool, n: int) -> str:
    parts = [stem, lang] + (["forced"] if forced else ["sdh"] if sdh else []) + \
        ([str(n)] if n else [])
    return ".".join(parts) + ".srt"


def _existing_slot(video: Path, lang: str, sdh: bool, forced: bool) -> dict[int, Path]:
    out = {}
    for n in range(MAX_SUFFIX + 1):
        p = video.with_name(slot_name(video.stem, lang, sdh, forced, n))
        if p.exists():
            out[n] = p
    # also an existing 2-letter / unflagged equivalent counts for comparison only
    return out


def plan(root: Path, langs: list[str], limit: int | None = None,
         include_no_video: bool = False, detector=detect_lang) -> dict[str, Any]:
    root = Path(root)
    dirs = sorted(d for d in root.iterdir() if d.is_dir() and not d.name.startswith(".")
                  and not d.name.startswith("_"))
    videos_by_title: dict[str, list[str]] = {}
    info: list[tuple[Path, list[Path], list[Path]]] = []
    for d in dirs:
        vids = sorted(p for p in d.iterdir() if is_video(p))
        if vids:
            videos_by_title.setdefault(_norm_title(d.name), []).append(d.name)
        srts = _dir_srts(d)
        orphans = [s for s in srts if not any(_paired(s, v) for v in vids)]
        if orphans:
            info.append((d, vids, orphans))
    if limit:
        info = info[:limit]
    out_dirs = []
    for d, vids, orphans in info:
        e: dict[str, Any] = {"dir": str(d), "name": d.name, "videos": [str(v) for v in vids],
                             "orphans": len(orphans), "actions": []}
        if not vids:
            sib = [n for n in videos_by_title.get(_norm_title(d.name), []) if n != d.name]
            e["status"] = "no_video"
            e["hint"] = (f"sibling with video: {', '.join(sib)}" if sib else None)
            for s in orphans:
                e["actions"].append({"src": str(s), "action": "quarantine",
                                     "reason": "no_video", "execute": include_no_video,
                                     "size": s.stat().st_size})
            out_dirs.append(e)
            continue
        if len(vids) > 1:
            e["status"] = "ambiguous"
            out_dirs.append(e)
            continue
        e["status"] = "pair"
        video = vids[0]
        subs = []
        for s in orphans:
            h = parse_hint(s, video.stem)
            src_of_lang = "name"
            if not h["lang"]:
                h["lang"], conf = detector(s)
                src_of_lang = f"text:{conf}" if h["lang"] else "unknown"
            subs.append((s, h, src_of_lang))
        # biggest first (fullest sub becomes the primary slot); SDH sorts into own slot
        subs.sort(key=lambda x: -x[0].stat().st_size)
        taken: dict[tuple[str, bool, bool], dict[int, tuple[Path, str]]] = {}
        for s, h, how in subs:
            base = {"src": str(s), "lang": h["lang"], "lang_from": how, "sdh": h["sdh"],
                    "forced": h["forced"], "size": s.stat().st_size, "execute": True}
            if not h["lang"] or h["lang"] not in langs:
                e["actions"].append(base | {"action": "quarantine", "reason":
                                            "unknown_lang" if not h["lang"] else "other_lang"})
                continue
            key = (h["lang"], h["sdh"] and not h["forced"], h["forced"])
            if key not in taken:
                taken[key] = {n: (p, _sha(p)) for n, p in
                              _existing_slot(video, h["lang"], key[1], key[2]).items()}
            slot = taken[key]
            sha = _sha(s)
            dup = next((p for p, hsh in slot.values() if hsh == sha), None)
            if dup is not None:
                e["actions"].append(base | {"action": "quarantine", "reason": "dup",
                                            "dup_of": str(dup)})
                continue
            free = next((n for n in range(MAX_SUFFIX + 1) if n not in slot), None)
            if free is None:
                e["actions"].append(base | {"action": "quarantine", "reason": "slot_full"})
                continue
            dst = video.with_name(slot_name(video.stem, h["lang"], key[1], key[2], free))
            slot[free] = (dst, sha)
            e["actions"].append(base | {"action": "rename", "dst": str(dst)})
        out_dirs.append(e)
    return {"root": str(root), "langs": langs, "include_no_video": include_no_video,
            "dirs": out_dirs, "summary": summarize(out_dirs)}


def summarize(dirs: list[dict[str, Any]]) -> dict[str, Any]:
    acts = [a for d in dirs for a in d["actions"]]
    return {
        "dirs": len(dirs),
        "pair_dirs": sum(d["status"] == "pair" for d in dirs),
        "orphans": sum(d["orphans"] for d in dirs),
        "paired": dict(Counter(a["lang"] + (".sdh" if a["sdh"] and not a["forced"] else
                                            ".forced" if a["forced"] else "")
                               for a in acts if a["action"] == "rename")),
        "dup_quarantined": sum(a.get("reason") == "dup" for a in acts),
        "slot_full_quarantined": sum(a.get("reason") == "slot_full" for a in acts),
        "other_lang_quarantined": dict(Counter(a.get("lang") or "unknown" for a in acts
                                               if a.get("reason") in ("other_lang",
                                                                      "unknown_lang"))),
        "no_video_dirs": sum(d["status"] == "no_video" for d in dirs),
        "no_video_srts": sum(len(d["actions"]) for d in dirs if d["status"] == "no_video"),
        "ambiguous_dirs": sum(d["status"] == "ambiguous" for d in dirs),
    }


def _rmdir_empty_subs(d: Path) -> list[str]:
    removed = []
    for sd in list(d.iterdir()):
        if sd.is_dir() and sd.name.lower() in SUBS_DIRS:
            for p in sorted(sd.rglob("*"), key=lambda x: -len(x.parts)) + [sd]:
                if p.is_dir():
                    try:
                        p.rmdir()  # only succeeds when empty
                        removed.append(str(p))
                    except OSError:
                        pass
    return removed


def execute(p: dict[str, Any], settings, db=None, source: str | None = "cli",
            say=None) -> dict[str, Any]:
    qroot = Path(settings.quarantine_root())
    batch = qroot / date.today().isoformat()
    rbatch = batch / "srt-pair"
    roots = [Path(p["root"]), Path(settings.media_root)]
    results = []
    removed: list[str] = []
    for d in p["dirs"]:
        if d["status"] == "ambiguous":
            continue
        video = d["videos"][0] if d["videos"] else None
        for a in d["actions"]:
            if not a["execute"]:
                continue
            src = Path(a["src"])
            r = dict(a)
            if not src.exists():
                results.append(r | {"result": "skipped", "why": "source gone"})
                continue
            try:
                if a["action"] == "rename":
                    dst = Path(a["dst"])
                    if dst.exists():  # never overwrite
                        results.append(r | {"result": "skipped", "why": "destination exists"})
                        continue
                    with changelog.change(db, "rename", "subtitle", old_path=src,
                                          new_path=dst, source=source,
                                          extra_lock_paths=[video] if video else [],
                                          check_lease=False,
                                          details={"reason": REASON, "lang": a["lang"],
                                                   "video": video}) as ch:
                        os.rename(src, dst)
                        ch.add_undo(lambda s=src, t=dst: os.rename(t, s))
                    rbatch.mkdir(parents=True, exist_ok=True)
                    _write_manifest(rbatch, {
                        "moved_at": datetime.now(UTC).isoformat(), "group": None,
                        "media_id": None, "size_bytes": a["size"], "keeper": video,
                        "path": str(src), "quarantine_path": str(dst),
                        "reason": REASON + "_rename", "lang": a["lang"]})
                else:
                    dst = _qpath(batch, src, roots)
                    if dst.exists():
                        dst = dst.with_name(
                            f"{dst.name}.{datetime.now(UTC).strftime('%H%M%S%f')}")
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    with changelog.change(db, "quarantine", "subtitle", old_path=src,
                                          new_path=dst, source=source, check_lease=False,
                                          details={"reason": f"{REASON}:{a['reason']}",
                                                   "lang": a.get("lang"),
                                                   "dup_of": a.get("dup_of")}) as ch:
                        os.rename(src, dst)
                        ch.add_undo(lambda s=src, t=dst: os.rename(t, s))
                    _write_manifest(batch, {
                        "moved_at": datetime.now(UTC).isoformat(), "group": None,
                        "media_id": None, "size_bytes": a["size"], "keeper": a.get("dup_of"),
                        "path": str(src), "quarantine_path": str(dst),
                        "reason": f"{REASON}:{a['reason']}", "lang": a.get("lang")})
                    log_action(qroot, db, source, action="quarantine", file_path=str(src),
                               quarantine_path=str(dst), size_bytes=a["size"],
                               reason=f"{REASON}:{a['reason']}")
                r |= {"result": "done", "to": str(dst)}
            except changelog.Busy as exc:
                r |= {"result": "skipped", "why": f"busy: {exc}"}
            except OSError as exc:
                r |= {"result": "error", "why": str(exc)}
            results.append(r)
            if say:
                say(f"{r['result']}: {src} -> {r.get('to', r.get('why'))}")
        removed += _rmdir_empty_subs(Path(d["dir"]))
    done = [r for r in results if r["result"] == "done"]
    return {"renamed": sum(r["action"] == "rename" for r in done),
            "quarantined": sum(r["action"] == "quarantine" for r in done),
            "skipped": sum(r["result"] == "skipped" for r in results),
            "errors": sum(r["result"] == "error" for r in results),
            "subs_dirs_removed": removed,
            "manifest": str(batch / "manifest.jsonl")
            if any(r["action"] == "quarantine" for r in done) else None,
            "rename_manifest": str(rbatch / "manifest.jsonl")
            if any(r["action"] == "rename" for r in done) else None,
            "results": results}


def format_plan(p: dict[str, Any], res: dict[str, Any] | None = None,
                sample: int = 25) -> str:
    s = p["summary"]
    L = [f"srt-pair  root={p['root']}  langs={','.join(p['langs'])}"
         + ("" if res else "  (DRY RUN - add --execute)"),
         f"dirs with orphan srts: {s['dirs']} (pairable {s['pair_dirs']}), orphans "
         f"{s['orphans']}",
         f"paired per language: {s['paired'] or '-'}",
         f"dup -> quarantine: {s['dup_quarantined']}   over cap -> quarantine: "
         f"{s['slot_full_quarantined']}",
         f"other/unknown language -> quarantine: {s['other_lang_quarantined'] or '-'}",
         f"no_video dirs: {s['no_video_dirs']} ({s['no_video_srts']} srts; "
         + ("quarantined" if p["include_no_video"] else "kept, use --include-no-video")
         + f")   ambiguous (several videos, skipped): {s['ambiguous_dirs']}", ""]
    for d in p["dirs"]:
        if d["status"] in ("no_video", "ambiguous"):
            L.append(f"  [{d['status']}] {d['name']} ({d['orphans']} srts)"
                     + (f"  hint: {d['hint']}" if d.get("hint") else "")
                     + (f"  videos: {len(d['videos'])}" if d["status"] == "ambiguous" else ""))
    rows = [(Path(a["src"]).relative_to(Path(p["root"])).as_posix(), a.get("lang") or "?",
             a["action"] + ("" if a["action"] == "rename" else f":{a['reason']}"),
             Path(a["dst"]).name if a.get("dst") else "")
            for d in p["dirs"] if d["status"] == "pair" for a in d["actions"]][:sample]
    if rows:
        L += ["", f"sample ({len(rows)}):"]
        w = [min(70, max(len(r[i]) for r in rows)) for i in range(3)]
        for r in rows:
            L.append(f"  {r[0][:70]:<{w[0]}}  {r[1]:<4} {r[2]:<{w[2]}}  {r[3]}")
    if res:
        L += ["", f"executed: renamed {res['renamed']}, quarantined {res['quarantined']}, "
              f"skipped {res['skipped']}, errors {res['errors']}, Subs dirs removed "
              f"{len(res['subs_dirs_removed'])}",
              f"undo: aibrarian duplicates restore --manifest {res['rename_manifest']} "
              "--execute" if res["rename_manifest"] else "",
              f"undo: aibrarian duplicates restore --manifest {res['manifest']} --execute"
              if res["manifest"] else ""]
    return "\n".join(x for x in L if x is not None) + "\n"
