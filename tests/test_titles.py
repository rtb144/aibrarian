import pytest
from fastapi.testclient import TestClient

from aibrarian import services
from aibrarian.api import create_app
from aibrarian.titles import TitleNotResolved, norm, rank, resolve, split_year

CAT = [
    {"media_id": 1, "title": "22 Jump Street", "year": 2014},
    {"media_id": 2, "title": "21 Jump Street", "year": 2012},
    {"media_id": 3, "title": "The Matrix", "year": 1999},
    {"media_id": 4, "title": "Alfie", "year": 1966},
    {"media_id": 5, "title": "Alfie", "year": 2004},
    {"media_id": 6, "title": "Amélie", "year": 2001},
    {"media_id": 7, "title": "Mr. & Mrs. Smith", "year": 2005},
]


def test_norm_and_year():
    assert norm("The Matrix!") == "matrix" and norm("Amélie") == "amelie"
    assert norm("Mr. & Mrs. Smith") == "mr and mrs smith"
    assert split_year("22 Jump Street (2014)") == ("22 Jump Street", 2014)
    assert split_year("1917") == ("1917", None)  # a title that is a year


@pytest.mark.parametrize("q,mid", [
    ("22 jump street", 1), ("22 JUMP STREET (2014)", 1), ("22 jump stret", 1),
    ("matrix", 3), ("the matrx", 3), ("amelie", 6), ("mr and mrs smith", 7),
    ("Alfie (1966)", 4), ("alfie 2004", 5),
])
def test_resolve_confident(q, mid):
    assert resolve(q, CAT).match["media_id"] == mid


def test_resolve_ambiguous_and_missing():
    r = resolve("Alfie", CAT)
    assert r.reason == "ambiguous" and {s["media_id"] for s in r.suggestions} >= {4, 5}
    r = resolve("Jump Street", CAT)
    assert r.match is None and {1, 2} <= {s["media_id"] for s in r.suggestions}
    r = resolve("Zzyzx Road", CAT)
    assert r.reason == "not_found" and r.match is None


def test_rank_prefix():
    assert [r["media_id"] for r in rank("22 j", CAT, 3)][0] == 1
    assert rank("mat", CAT, 1)[0]["media_id"] == 3


class TitleDB:
    def searchable_titles(self):
        return CAT

    def get_media(self, mid):
        t = next(x for x in CAT if x["media_id"] == mid)
        return {"id": mid, "title": t["title"], "path": f"/m/{mid}.mkv", "duration_s": 100}


@pytest.fixture
def tclient(ctx, monkeypatch):
    ctx.db = TitleDB()
    seen = {}

    def fake_search(c, q, limit=10, mode="hybrid", media_id=None):
        seen["media_id"] = media_id
        return [{"segment_id": 1, "media_id": media_id or 3, "title": "x", "start": 1.0,
                 "end": 2.0, "text": q, "score": 0.9, "path": "/m"}]

    monkeypatch.setattr(services, "search", fake_search)
    c = TestClient(create_app(ctx, migrate=False))
    c.seen = seen
    return c


def test_search_with_title(tclient):
    r = tclient.get("/search", params={"q": "hello", "title": "22 jump stret"})
    assert r.status_code == 200 and isinstance(r.json(), list)
    assert r.headers["X-Media-Id"] == "1" and r.headers["X-Resolved-Title"] == "22 Jump Street (2014)"
    assert tclient.seen["media_id"] == 1
    r = tclient.get("/search/text", params={"q": "hello", "title": "matrix"})
    assert r.headers["X-Media-Id"] == "3"
    r = tclient.get("/search/semantic", params={"q": "hello", "movie_id": 2})
    assert r.status_code == 200 and "X-Media-Id" not in r.headers and tclient.seen["media_id"] == 2


def test_search_title_errors(tclient):
    r = tclient.get("/search", params={"q": "hello", "title": "Alfie"})
    assert r.status_code == 422 and len(r.json()["detail"]["suggestions"]) >= 2
    r = tclient.get("/search", params={"q": "hello", "title": "Zzyzx Road"})
    assert r.status_code == 404 and r.json()["detail"]["reason"] == "not_found"


def test_autocomplete_and_ui(tclient):
    rows = tclient.get("/titles/autocomplete", params={"q": "jump"}).json()
    assert {r["media_id"] for r in rows[:2]} == {1, 2} and rows[0]["label"].endswith(")")
    assert tclient.get("/titles/resolve", params={"title": "amelie"}).json()["media_id"] == 6
    html = tclient.get("/search/ui").text
    assert "datalist" in html and "../titles/autocomplete" in html and "Make clip" in html


def test_clip_uses_resolver(ctx):
    ctx.db = TitleDB()
    with pytest.raises(TitleNotResolved):
        services.resolve_media(ctx, None, "Alfie", None)
    assert services.resolve_media(ctx, None, "alfie (2004)", None)["id"] == 5
    c = TestClient(create_app(ctx, migrate=False))
    r = c.post("/clip", json={"phrase": "hi", "title": "Alfie"})
    assert r.status_code == 422 and r.json()["detail"]["suggestions"]
