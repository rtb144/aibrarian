import json

import httpx
import pytest

from aibrarian.jellyfin import JellyfinClient
from aibrarian.logging_setup import JsonFormatter
from aibrarian.ollama import OllamaClient, parse_json_reply
from aibrarian.vectors import VectorStore


def mock(handler, base="http://x"):
    return httpx.Client(base_url=base, transport=httpx.MockTransport(handler))


def test_parse_json_reply_variants():
    assert parse_json_reply('<think>hmm</think>{"a": 1}') == {"a": 1}
    assert parse_json_reply('```json\n{"a": 2}\n```') == {"a": 2}
    assert parse_json_reply('sure! {"a": 3} done') == {"a": 3}
    with pytest.raises(ValueError):
        parse_json_reply("no json")


def test_ollama_embed_prefixes():
    seen = {}

    def h(req):
        seen.update(json.loads(req.content))
        return httpx.Response(200, json={"embeddings": [[0.1, 0.2]]})

    c = OllamaClient("http://x", "nomic-embed-text", "qwen3:14b", client=mock(h))
    assert c.embed_query("space") == [0.1, 0.2]
    assert seen["input"] == ["search_query: space"] and seen["model"] == "nomic-embed-text"
    assert c.embed([]) == []


def test_ollama_generate_schema():
    def h(req):
        body = json.loads(req.content)
        assert body["think"] is False and body["format"] == {"type": "object"}
        return httpx.Response(200, json={"response": '{"kind": "movie"}'})

    c = OllamaClient("http://x", "e", "l", client=mock(h))
    assert c.generate_json("p", {"type": "object"}) == {"kind": "movie"}


def test_vector_store_contract():
    calls = []

    def h(req):
        calls.append((req.method, req.url.path, json.loads(req.content or b"null")))
        if req.method == "GET":
            return httpx.Response(404)
        if req.url.path.endswith("/count"):
            return httpx.Response(200, json={"result": {"count": 3}})
        if req.url.path.endswith("/search"):
            return httpx.Response(200, json={"result": [{"id": 5, "score": 0.8, "payload": {}}]})
        return httpx.Response(200, json={"result": {}})

    vs = VectorStore("http://q", "movie_segments", 768, client=mock(h, "http://q"))
    vs.ensure_collection()
    assert calls[1][2] == {"vectors": {"size": 768, "distance": "Cosine"}}
    assert calls[2][2] == {"field_name": "movie_id", "field_schema": "integer"}
    vs.upsert([1, 2], [[0.0], [1.0]], [{"movie_id": 9}, {"movie_id": 9}])
    assert calls[-1][2]["points"][0] == {"id": 1, "vector": [0.0], "payload": {"movie_id": 9}}
    hits = vs.search([0.1], 5, media_id=9, min_score=0.5)
    body = calls[-1][2]
    assert hits[0]["id"] == 5 and body["score_threshold"] == 0.5
    assert body["filter"]["must"][0] == {"key": "movie_id", "match": {"value": 9}}
    assert vs.count(9) == 3
    with pytest.raises(ValueError):
        vs.upsert([1], [], [])


def test_jellyfin_search_handles_errors():
    def h(req):
        assert req.headers["X-Emby-Token"] == "k"
        return httpx.Response(500)

    jf = JellyfinClient("http://j", "k", client=mock(h, "http://j"))
    jf.http.headers["X-Emby-Token"] = "k"
    assert jf.search("Alien") == [] and jf.search("") == []


def test_json_formatter():
    import logging

    rec = logging.makeLogRecord({"msg": "hi %s", "args": ("x",), "levelname": "INFO",
                                 "name": "t", "media_id": 4})
    out = json.loads(JsonFormatter("svc").format(rec))
    assert out["msg"] == "hi x" and out["media_id"] == 4 and out["service"] == "svc"
