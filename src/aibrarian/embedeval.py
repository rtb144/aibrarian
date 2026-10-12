"""Side-by-side embedding model A/B evaluation (no production changes).

``build``: pick N transcribed movies (deterministic: evenly spaced by duration), embed their
segments and windows with the baseline (``EMBED_MODEL``) and a candidate model into SEPARATE
Qdrant collections ``eval_<model>_segments`` / ``eval_<model>_windows``. Small batches, a pause
between batches, short ``keep_alive``; resumable per (model, movie) via ``state.json``.

``run``: a query set generated from the transcripts (exact quotes, LLM paraphrases,
LLM "scene where ..." descriptions) with ground truth (movie + time), plus optional user
queries; recall@1/5/10 and MRR (correct movie, within ±``tolerance_s``), median latency,
embed throughput and GPU memory (``/api/ps``). Reports in ``<report_dir>/embed-eval/``.

``cleanup``: drop every ``eval_*`` collection. Production collections are never written.
"""

from __future__ import annotations

import json
import random
import re
import statistics
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .ollama import embed_prefixes
from .vectors import VectorStore
from .windows import make_windows, window_point_id

QUERY_TYPES = ("exact", "paraphrase", "scene")
PARAPHRASE_SCHEMA = {"type": "object", "properties": {"text": {"type": "string"}},
                     "required": ["text"]}


def slug(model: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", model.lower()).strip("_")


def collections(model: str) -> tuple[str, str]:
    return f"eval_{slug(model)}_segments", f"eval_{slug(model)}_windows"


def is_hit(hit: dict[str, Any], gt: dict[str, Any], tol: float) -> bool:
    p = hit.get("payload") or hit
    if int(p.get("movie_id", -1)) != int(gt["movie_id"]):
        return False
    start, end = float(p.get("start", 0)), float(p.get("end", p.get("start", 0)))
    g0 = float(gt["start"])
    g1 = float(gt.get("end", g0))
    return start - tol <= g1 and end + tol >= g0


def metrics(ranks: list[int | None]) -> dict[str, Any]:
    n = len(ranks)
    if not n:
        return {"n": 0}
    r = lambda k: round(sum(1 for x in ranks if x is not None and x <= k) / n, 3)  # noqa: E731
    return {"n": n, "r@1": r(1), "r@5": r(5), "r@10": r(10),
            "mrr": round(sum(1 / x for x in ranks if x) / n, 3)}


class Harness:
    def __init__(self, s, db, ollama, store_factory: Callable[[str, int], Any] | None = None,
                 out_dir: Path | None = None, sleep: Callable[[float], None] = time.sleep,
                 say: Callable[[str], None] = print):
        self.s, self.db, self.ollama = s, db, ollama
        self.store = store_factory or (lambda name, dim: VectorStore(s.qdrant_url, name, dim))
        self.out = Path(out_dir or Path(s.report_dir) / "embed-eval")
        self.sleep, self.say = sleep, say
        self.baseline = s.embed_model
        self._embed_s = 0.0

    # -- state ------------------------------------------------------------------
    @property
    def state_path(self) -> Path:
        return self.out / "state.json"

    def state(self) -> dict[str, Any]:
        if self.state_path.exists():
            return json.loads(self.state_path.read_text())
        return {"movies": [], "models": {}}

    def save(self, st: dict[str, Any]) -> None:
        self.out.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, indent=1))
        tmp.replace(self.state_path)

    # -- build ------------------------------------------------------------------
    def pick_movies(self, n: int) -> list[dict[str, Any]]:
        """Evenly spaced by duration over all movies with >= 40 segments (deterministic)."""
        rows = sorted(self.db.embed_eval_candidates(self.s.canonical_roots.get("movie",
                                                                               "01-movies")),
                      key=lambda r: (r.get("duration_s") or 0, r["id"]))
        if len(rows) <= n:
            return rows
        step = len(rows) / n
        return [rows[int(i * step + step / 2)] for i in range(n)]

    def _embed(self, model: str, texts: list[str], batch: int, pause: float,
               keep_alive: str) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), batch):
            t0 = time.monotonic()
            out += self.ollama.embed(texts[i:i + batch], model=model, keep_alive=keep_alive)
            self._embed_s += time.monotonic() - t0
            if pause:
                self.sleep(pause)
        return out

    def build(self, model: str, movies: int = 20, batch: int = 16, pause: float = 0.2,
              keep_alive: str = "2m", reselect: bool = False,
              baseline: bool = True) -> dict[str, Any]:
        st = self.state()
        if reselect or not st["movies"]:
            picked = self.pick_movies(movies)
            st = {"movies": [{"id": r["id"], "title": r.get("title"),
                              "duration_s": r.get("duration_s")} for r in picked],
                  "models": {}}
            self.save(st)
            self.say(f"picked {len(picked)} movies: "
                     + ", ".join(f"{m['id']}:{m['title']}" for m in st["movies"]))
        models = list(dict.fromkeys(([self.baseline] if baseline else []) + [model]))
        for m in models:
            ms = st["models"].setdefault(m, {"done": [], "texts": 0, "seconds": 0.0, "dim": None})
            dim = ms["dim"] or len(self.ollama.embed(["dimension probe"], model=m,
                                                     keep_alive=keep_alive)[0])
            ms["dim"] = dim
            seg_c, win_c = collections(m)
            segs_store, wins_store = self.store(seg_c, dim), self.store(win_c, dim)
            segs_store.ensure_collection()
            wins_store.ensure_collection()
            for k, mv in enumerate(st["movies"], 1):
                if mv["id"] in ms["done"]:
                    continue
                segs = self.db.segments_for_media(mv["id"])
                wins = make_windows(segs, self.s.window_size, self.s.window_stride) if segs else []
                base = {"movie_id": mv["id"], "title": mv.get("title")}
                self._embed_s = 0.0
                sv = self._embed(m, [g["text"] for g in segs], batch, pause, keep_alive)
                wv = self._embed(m, [w.text for w in wins], batch, pause, keep_alive)
                el = self._embed_s
                segs_store.delete_media(mv["id"])
                wins_store.delete_media(mv["id"])
                segs_store.upsert([g["id"] for g in segs], sv,
                                  [base | {"seq": g["seq"], "start": g["start_s"],
                                           "end": g["end_s"], "text": g["text"][:400]}
                                   for g in segs])
                wins_store.upsert([window_point_id(mv["id"], w.index) for w in wins], wv,
                                  [base | {"start": w.start, "end": w.end, "text": w.text[:600]}
                                   for w in wins])
                ms["done"].append(mv["id"])
                ms["texts"] += len(segs) + len(wins)
                ms["seconds"] = round(ms["seconds"] + max(el, 1e-6), 3)
                self.save(st)
                self.say(f"[{m}] movie {k}/{len(st['movies'])} {mv.get('title')!r}: "
                         f"{len(segs)} segments + {len(wins)} windows "
                         f"({(len(segs) + len(wins)) / max(el, 1e-6):.0f} texts/s)")
        return st

    # -- queries ----------------------------------------------------------------
    def make_queries(self, per_type: int = 20, seed: int = 7, regen: bool = False,
                     llm: bool = True) -> list[dict[str, Any]]:
        path = self.out / "queries.json"
        if path.exists() and not regen:
            return json.loads(path.read_text())
        rng = random.Random(seed)
        movies = self.state()["movies"]
        if not movies:
            raise RuntimeError("no eval movies; run `aibrarian embed-eval build` first")
        segs = {m["id"]: self.db.segments_for_media(m["id"]) for m in movies}
        out: list[dict[str, Any]] = []
        for qt in QUERY_TYPES:
            tries = 0
            got = 0
            while got < per_type and tries < per_type * 6:
                tries += 1
                mv = movies[(got + tries) % len(movies)]
                ss = segs[mv["id"]]
                pool = [g for g in ss[len(ss) // 20: len(ss) - len(ss) // 20]
                        if len(g["text"].split()) >= 8] or ss
                if not pool:
                    continue
                g = rng.choice(pool)
                q: dict[str, Any] = {"type": qt, "movie_id": mv["id"], "title": mv.get("title"),
                                     "start": g["start_s"], "end": g["end_s"],
                                     "source": g["text"]}
                if qt == "exact":
                    q["query"] = g["text"].strip()
                elif not llm:
                    continue
                elif qt == "paraphrase":
                    q["query"] = self._llm(
                        "Paraphrase this line of movie dialogue so it means the same thing but "
                        "shares as few words as possible with the original. Reply as JSON "
                        f'{{"text": "..."}}.\n\nLine: {g["text"]}')
                else:
                    i = ss.index(g)
                    win = ss[max(0, i - 4): i + 5]
                    q |= {"start": win[0]["start_s"], "end": win[-1]["end_s"],
                          "source": " ".join(w["text"] for w in win)}
                    q["query"] = self._llm(
                        "Here is a short stretch of movie dialogue. Describe what happens in it "
                        "in ONE sentence that starts with 'scene where', as a viewer searching "
                        "for it would. Do not quote the dialogue and do not name the movie. "
                        'Reply as JSON {"text": "..."}.\n\nDialogue: ' + q["source"])
                if q.get("query"):
                    out.append(q)
                    got += 1
        self.out.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(out, indent=1))
        return out

    def _llm(self, prompt: str) -> str | None:
        try:
            return (self.ollama.generate_json(prompt, PARAPHRASE_SCHEMA).get("text") or "").strip()
        except Exception as exc:
            self.say(f"LLM query generation failed: {exc}")
            return None

    @staticmethod
    def user_queries(path: Path) -> list[dict[str, Any]]:
        """JSON list or JSONL of ``{"query", "movie_id"?, "start"?, "end"?}``."""
        text = Path(path).read_text()
        rows = json.loads(text) if text.lstrip().startswith("[") else [
            json.loads(x) for x in text.splitlines() if x.strip()]
        return [{"type": "user"} | r for r in rows if r.get("query")]

    # -- run --------------------------------------------------------------------
    def search(self, model: str, query: str, k: int = 10) -> dict[str, Any]:
        st = self.state()
        dim = (st["models"].get(model) or {}).get("dim") or self.s.embed_dim
        seg_c, win_c = collections(model)
        t0 = time.monotonic()
        v = self.ollama.embed_query(query, model=model, keep_alive="2m")
        t1 = time.monotonic()
        segs = self.store(seg_c, dim).search(v, k)
        wins = self.store(win_c, dim).search(v, k)
        t2 = time.monotonic()
        return {"segments": segs, "windows": wins, "embed_ms": (t1 - t0) * 1000,
                "search_ms": (t2 - t1) * 1000}

    def run(self, models: list[str] | None = None, user_file: Path | None = None,
            per_type: int = 20, regen: bool = False, tolerance_s: float = 30.0,
            k: int = 10) -> dict[str, Any]:
        st = self.state()
        models = models or list(st["models"])
        if not models:
            raise RuntimeError("nothing built; run `aibrarian embed-eval build` first")
        qs = self.make_queries(per_type, regen=regen)
        if user_file:
            qs = qs + self.user_queries(user_file)
        res: dict[str, Any] = {"generated_at": datetime.now(UTC).isoformat(),
                               "movies": st["movies"], "tolerance_s": tolerance_s,
                               "queries": len(qs), "models": {}, "per_query": []}
        per_query = {i: {"query": q["query"], "type": q["type"], "movie_id": q.get("movie_id"),
                         "start": q.get("start")} for i, q in enumerate(qs)}
        for m in models:
            ranks: dict[str, dict[str, list]] = {ix: {} for ix in ("segments", "windows")}
            lat = []
            for i, q in enumerate(qs):
                r = self.search(m, q["query"], k)
                lat.append(r["embed_ms"] + r["search_ms"])
                for ix in ("segments", "windows"):
                    rank = None
                    if q.get("movie_id") is not None and q.get("start") is not None:
                        rank = next((n for n, h in enumerate(r[ix], 1)
                                     if is_hit(h, q, tolerance_s)), None)
                        ranks[ix].setdefault(q["type"], []).append(rank)
                        ranks[ix].setdefault("all", []).append(rank)
                    per_query[i].setdefault(m, {})[ix] = rank
            ms = st["models"].get(m) or {}
            vram = None
            try:
                for lm in self.ollama.loaded_models():
                    if lm.get("name", "").split(":")[0] == m.split(":")[0] or lm.get("name") == m:
                        vram = round(lm.get("size_vram", 0) / 2**20)
            except Exception:
                pass
            res["models"][m] = {
                "prefixes": dict(zip(("query", "document"), embed_prefixes(m), strict=True)),
                "dim": ms.get("dim"),
                "throughput_texts_s": round(ms["texts"] / ms["seconds"], 1)
                if ms.get("seconds") else None,
                "median_latency_ms": round(statistics.median(lat), 1) if lat else None,
                "gpu_vram_mb": vram,
                "metrics": {ix: {t: metrics(v) for t, v in sorted(ranks[ix].items())}
                            for ix in ranks}}
        res["per_query"] = list(per_query.values())
        self.write_report(res)
        return res

    def write_report(self, res: dict[str, Any]) -> Path:
        self.out.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        md = format_report(res)
        for name in (f"report-{ts}", "latest"):
            (self.out / f"{name}.json").write_text(json.dumps(res, indent=1, default=str))
            (self.out / f"{name}.md").write_text(md)
        return self.out / f"report-{ts}.md"

    def latest(self) -> dict[str, Any] | None:
        p = self.out / "latest.json"
        return json.loads(p.read_text()) if p.exists() else None

    # -- cleanup ----------------------------------------------------------------
    def cleanup(self) -> list[str]:
        probe = self.store("eval_probe", self.s.embed_dim)
        dropped = []
        for name in probe.collections():
            if name.startswith("eval_"):
                self.store(name, self.s.embed_dim).drop()
                dropped.append(name)
        st = self.state()
        if st["models"]:
            st["models"] = {}
            self.save(st)
        return dropped


def summary_rows(res: dict[str, Any]) -> list[list[str]]:
    rows = []
    for m, r in res["models"].items():
        for ix, by in r["metrics"].items():
            for t, v in by.items():
                if v.get("n"):
                    rows.append([m, ix, t, str(v["n"]), f"{v['r@1']:.2f}", f"{v['r@5']:.2f}",
                                 f"{v['r@10']:.2f}", f"{v['mrr']:.2f}"])
    return rows


def format_report(res: dict[str, Any]) -> str:
    out = [f"# Embedding A/B report ({res['generated_at']})", "",
           f"{len(res['movies'])} movies, {res['queries']} queries, hit = right movie within "
           f"±{res['tolerance_s']:.0f}s.", "",
           "| model | dim | median latency ms | embed texts/s | GPU VRAM MB | query prefix | "
           "document prefix |", "|---|---|---|---|---|---|---|"]
    for m, r in res["models"].items():
        out.append(f"| {m} | {r['dim']} | {r['median_latency_ms']} | {r['throughput_texts_s']} "
                   f"| {r['gpu_vram_mb']} | `{r['prefixes']['query']}` | "
                   f"`{r['prefixes']['document']}` |")
    out += ["", "| model | index | query type | n | R@1 | R@5 | R@10 | MRR |",
            "|---|---|---|---|---|---|---|---|"]
    out += ["| " + " | ".join(r) + " |" for r in summary_rows(res)]
    out += ["", "Movies: " + ", ".join(f"{m['title']} ({m['id']})" for m in res["movies"])]
    return "\n".join(out) + "\n"


def format_table(res: dict[str, Any]) -> str:
    head = ["model", "index", "type", "n", "R@1", "R@5", "R@10", "MRR"]
    rows = [head] + summary_rows(res)
    w = [max(len(r[i]) for r in rows) for i in range(len(head))]
    lines = ["  ".join(c.ljust(w[i]) for i, c in enumerate(r)) for r in rows]
    for m, r in res["models"].items():
        lines.append(f"{m}: median latency {r['median_latency_ms']} ms, "
                     f"{r['throughput_texts_s']} texts/s, VRAM {r['gpu_vram_mb']} MB")
    return "\n".join(lines)
