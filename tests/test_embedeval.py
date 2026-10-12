import hashlib
import json
import math

import httpx
from fastapi.testclient import TestClient

from aibrarian import embedeval
from aibrarian.api import create_app
from aibrarian.ollama import OllamaClient, embed_prefixes

WORDS = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel", "india",
         "juliet", "kilo", "lima", "mike", "november", "oscar", "papa", "quebec", "romeo",
         "sierra", "tango", "uniform", "victor", "whiskey", "xray"]


def _bow(text, dim=64):
    v = [0.0] * dim
    for w in text.lower().replace(":", " ").replace("|", " ").split():
        v[int(hashlib.md5(w.encode()).hexdigest(), 16) % dim] += 1
    n = math.sqrt(sum(x * x for x in v)) or 1
    return [x / n for x in v]


class EvalOllama:
    """'good' embeds by bag of words, the baseline by a hash of the whole text (useless
    for paraphrases, exact for identical strings modulo prefix)."""

    def __init__(self):
        self.calls = []

    def embed(self, texts, prefix=None, model=None, keep_alive=None):
        self.calls.append((model, len(texts), keep_alive))
        q, d = embed_prefixes(model)
        strip = lambda t: t  # noqa: E731
        return [_bow(strip(t)) if model == "good" else _bow(t[: len(t) // 2]) for t in texts]

    def embed_query(self, text, model=None, keep_alive=None):
        return self.embed([text], model=model, keep_alive=keep_alive)[0]

    def generate_json(self, prompt, schema=None, num_ctx=4096):
        src = prompt.rsplit(": ", 1)[-1]
        return {"text": " ".join(reversed(src.split()))}

    def loaded_models(self):
        return [{"name": "good:latest", "size_vram": 600 * 2**20}]


class MemStores:
    def __init__(self):
        self.cols = {}

    def __call__(self, name, dim):
        stores = self

        class S:
            def ensure_collection(self):
                stores.cols.setdefault(name, {})

            def delete_media(self, mid):
                c = stores.cols.setdefault(name, {})
                for k in [k for k, (_, p) in c.items() if p["movie_id"] == mid]:
                    del c[k]

            def upsert(self, ids, vecs, pays):
                stores.cols.setdefault(name, {}).update(
                    {i: (v, p) for i, v, p in zip(ids, vecs, pays, strict=True)})

            def search(self, vec, limit=10):
                pts = stores.cols.get(name, {})
                sc = sorted(((sum(a * b for a, b in zip(vec, v, strict=True)), i, p)
                             for i, (v, p) in pts.items()), key=lambda x: -x[0])
                return [{"id": i, "score": s, "payload": p} for s, i, p in sc[:limit]]

            def collections(self):
                return list(stores.cols)

            def drop(self):
                return stores.cols.pop(name, None) is not None
        return S()


class EvalDB:
    def __init__(self, n_movies=6, n_segs=60):
        self.segs = {}
        for m in range(1, n_movies + 1):
            self.segs[m] = [{"id": m * 1000 + i, "media_id": m, "seq": i, "start_s": i * 10.0,
                             "end_s": i * 10.0 + 8,
                             "text": " ".join(WORDS[(m * 7 + i * 3 + j) % len(WORDS)]
                                              for j in range(9)) + f" m{m}s{i}"}
                            for i in range(n_segs)]
        self.cands = [{"id": m, "title": f"Movie {m}", "duration_s": 600.0 * m,
                       "segments": n_segs} for m in self.segs]
        self.cands.append({"id": 99, "title": "Short", "duration_s": 1.0, "segments": 0})

    def embed_eval_candidates(self, root):
        return [c for c in self.cands if c["segments"] >= 40]

    def segments_for_media(self, mid):
        return self.segs.get(mid, [])


def harness(settings, stores=None, ollama=None):
    settings.embed_model = "base"
    return embedeval.Harness(settings, EvalDB(), ollama or EvalOllama(), stores or MemStores(),
                             sleep=lambda s: None, say=lambda m: None)


def test_prefix_registry_default_unchanged():
    assert embed_prefixes("nomic-embed-text") == ("search_query: ", "search_document: ")
    assert embed_prefixes("nomic-embed-text:latest") == ("search_query: ", "search_document: ")
    assert embed_prefixes("embeddinggemma-2:740m") == ("task: search result | query: ",
                                                       "title: none | text: ")
    assert embed_prefixes("something-else") == ("search_query: ", "search_document: ")
    seen = []

    def h(req):
        seen.append(json.loads(req.content))
        return httpx.Response(200, json={"embeddings": [[0.1]]})
    c = OllamaClient("http://x", "nomic-embed-text", "q",
                     client=httpx.Client(base_url="http://x", transport=httpx.MockTransport(h)))
    c.embed(["a"])
    c.embed_query("b", model="embeddinggemma-2", keep_alive="1m")
    assert seen[0] == {"model": "nomic-embed-text", "input": ["search_document: a"]}
    assert seen[1] == {"model": "embeddinggemma-2", "keep_alive": "1m",
                       "input": ["task: search result | query: b"]}


def test_pick_movies_deterministic_and_varied(settings):
    h = harness(settings)
    a = [m["id"] for m in h.pick_movies(3)]
    assert a == [m["id"] for m in h.pick_movies(3)] and len(set(a)) == 3
    assert a == [2, 4, 6]                                  # spread over the duration range


def test_build_resumable_small_batches_separate_collections(settings):
    stores, oll = MemStores(), EvalOllama()
    h = harness(settings, stores, oll)
    st = h.build("good", movies=4, batch=16, keep_alive="2m")
    assert set(stores.cols) == {"eval_base_segments", "eval_base_windows",
                                "eval_good_segments", "eval_good_windows"}
    assert not any(c.startswith("movie_") for c in stores.cols)        # production untouched
    assert len(stores.cols["eval_good_segments"]) == 4 * 60
    assert max(n for _, n, _ in oll.calls) <= 16 and {k for *_, k in oll.calls} == {"2m"}
    assert st["models"]["good"]["dim"] == 64 and len(st["models"]["good"]["done"]) == 4
    n = len(oll.calls)
    h.build("good", movies=4)                                          # resume: nothing left
    assert len(oll.calls) == n


def test_run_metrics_report_and_cleanup(settings, tmp_path):
    stores = MemStores()
    h = harness(settings, stores)
    h.build("good", movies=4)
    uq = tmp_path / "q.jsonl"
    uq.write_text(json.dumps({"query": "alpha bravo", "movie_id": 1, "start": 0}) + "\n"
                  + json.dumps({"query": "no ground truth"}) + "\n")
    res = h.run(user_file=uq, per_type=6)
    good = res["models"]["good"]
    assert good["metrics"]["segments"]["exact"]["r@1"] == 1.0          # exact quotes found
    assert good["metrics"]["segments"]["paraphrase"]["r@5"] >= 0.8    # bag of words: reversed
    assert good["metrics"]["segments"]["all"]["n"] == 6 * 3 + 1
    assert res["models"]["base"]["metrics"]["segments"]["paraphrase"]["r@10"] < 1.0
    assert good["gpu_vram_mb"] == 600 and good["prefixes"]["query"] == "search_query: "
    assert good["median_latency_ms"] is not None
    assert {q["type"] for q in json.loads((h.out / "queries.json").read_text())} == {
        "exact", "paraphrase", "scene"}
    md = (h.out / "latest.md").read_text()
    assert "| good | segments | exact |" in md and "R@1" in embedeval.format_table(res)
    # queries are cached (same set next run)
    assert h.make_queries(6) == json.loads((h.out / "queries.json").read_text())
    assert sorted(h.cleanup()) == sorted(["eval_base_segments", "eval_base_windows",
                                          "eval_good_segments", "eval_good_windows"])
    assert not stores.cols and h.state()["models"] == {} and h.state()["movies"]


def test_is_hit_tolerance():
    gt = {"movie_id": 3, "start": 100.0, "end": 105.0}
    assert embedeval.is_hit({"payload": {"movie_id": 3, "start": 130, "end": 140}}, gt, 30)
    assert not embedeval.is_hit({"payload": {"movie_id": 3, "start": 140, "end": 150}}, gt, 30)
    assert not embedeval.is_hit({"payload": {"movie_id": 4, "start": 100, "end": 105}}, gt, 30)
    assert embedeval.metrics([1, 3, None, 12]) == {"n": 4, "r@1": 0.25, "r@5": 0.5,
                                                   "r@10": 0.5, "mrr": round((1 + 1 / 3 + 1 / 12) / 4, 3)}


def test_ui_and_query_endpoint(settings, ctx, monkeypatch):
    stores = MemStores()
    h = harness(settings, stores)
    h.build("good", movies=2)
    h.run(per_type=2)
    ctx.settings = settings
    ctx.ollama = EvalOllama()
    real = embedeval.Harness.__init__

    def init(self, s, db, ollama, store_factory=None, **kw):
        real(self, s, EvalDB(), ollama, stores, **kw)
    monkeypatch.setattr(embedeval.Harness, "__init__", init)
    cl = TestClient(create_app(ctx))
    assert "Embedding A/B" in cl.get("/embed-eval/ui").text
    lat = cl.get("/embed-eval/latest").json()
    assert lat["report"]["queries"] == 6 and "R@1" in lat["markdown"]
    r = cl.get("/embed-eval/query", params={"q": "alpha bravo charlie"}).json()
    assert set(r) == {"base", "good"} and len(r["good"]["windows"]) == 5
    assert r["good"]["segments"][0]["text"]
