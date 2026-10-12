"""Minimal Ollama HTTP client (embeddings + JSON chat)."""

from __future__ import annotations

import json
import re
from typing import Any

import httpx

from .config import Settings

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)

# Task prefixes per embedding model family: (query prefix, document prefix).
# nomic-embed-text: model card "search_query: " / "search_document: ".
# embeddinggemma(-2): Google model card (huggingface.co/google/embeddinggemma-2, prompts
#   Retrieval-query / Retrieval-document): "task: search result | query: " /
#   "title: none | text: " (no title -> "none").
EMBED_PREFIXES: dict[str, tuple[str, str]] = {
    "nomic-embed-text": ("search_query: ", "search_document: "),
    "embeddinggemma": ("task: search result | query: ", "title: none | text: "),
    "embeddinggemma-2": ("task: search result | query: ", "title: none | text: "),
}
DEFAULT_PREFIXES = EMBED_PREFIXES["nomic-embed-text"]  # historical behaviour for unknown models


def embed_prefixes(model: str) -> tuple[str, str]:
    """``(query_prefix, document_prefix)`` for an Ollama model name (tag ignored)."""
    base = model.split("/")[-1].split(":")[0].lower()
    return EMBED_PREFIXES.get(base, DEFAULT_PREFIXES)


class OllamaClient:
    def __init__(self, base_url: str, embed_model: str, llm_model: str,
                 timeout: float = 300.0, client: httpx.Client | None = None):
        self.embed_model = embed_model
        self.llm_model = llm_model
        self.http = client or httpx.Client(base_url=base_url, timeout=timeout)

    @classmethod
    def from_settings(cls, s: Settings) -> OllamaClient:
        return cls(s.ollama_url, s.embed_model, s.llm_model, s.llm_timeout_s)

    def embed(self, texts: list[str], prefix: str | None = None, model: str | None = None,
              keep_alive: str | None = None) -> list[list[float]]:
        """Embed documents with the model's task prefix (see :data:`EMBED_PREFIXES`)."""
        if not texts:
            return []
        model = model or self.embed_model
        prefix = embed_prefixes(model)[1] if prefix is None else prefix
        body: dict[str, Any] = {"model": model, "input": [prefix + t for t in texts]}
        if keep_alive is not None:
            body["keep_alive"] = keep_alive
        r = self.http.post("/api/embed", json=body)
        r.raise_for_status()
        return r.json()["embeddings"]

    def embed_query(self, text: str, model: str | None = None,
                    keep_alive: str | None = None) -> list[float]:
        model = model or self.embed_model
        return self.embed([text], embed_prefixes(model)[0], model, keep_alive)[0]

    def loaded_models(self) -> list[dict[str, Any]]:
        """``/api/ps``: models in memory with ``size_vram``."""
        r = self.http.get("/api/ps")
        r.raise_for_status()
        return r.json().get("models") or []

    def chat_json(self, system: str, user: str) -> dict[str, Any]:
        """Ask the LLM for a JSON object; tolerant of <think> blocks / code fences."""
        r = self.http.post("/api/chat", json={
            "model": self.llm_model,
            "stream": False,
            "format": "json",
            "options": {"temperature": 0.1},
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
        })
        r.raise_for_status()
        return parse_json_reply(r.json()["message"]["content"])

    def generate_json(self, prompt: str, schema: dict[str, Any] | None = None,
                      num_ctx: int = 4096) -> dict[str, Any]:
        """Schema-constrained generation (Ollama structured outputs), thinking off."""
        r = self.http.post("/api/generate", json={
            "model": self.llm_model, "stream": False, "prompt": prompt,
            "format": schema or "json", "think": False, "keep_alive": "30m",
            "options": {"temperature": 0.1, "num_ctx": num_ctx},
        })
        r.raise_for_status()
        return parse_json_reply(r.json().get("response", ""))

    def chat_text(self, prompt: str, num_ctx: int = 32768) -> str:
        r = self.http.post("/api/chat", json={
            "model": self.llm_model, "stream": False, "think": False,
            "options": {"temperature": 0.2, "num_ctx": num_ctx},
            "messages": [{"role": "user", "content": prompt}]})
        r.raise_for_status()
        return _THINK_RE.sub("", r.json()["message"]["content"]).strip()

    def ping(self) -> bool:
        try:
            return self.http.get("/api/tags").status_code == 200
        except httpx.HTTPError:
            return False


def parse_json_reply(content: str) -> dict[str, Any]:
    content = _THINK_RE.sub("", content).strip()
    content = re.sub(r"^```(?:json)?|```$", "", content, flags=re.MULTILINE).strip()
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", content, re.DOTALL)
        if not m:
            raise ValueError(f"LLM did not return JSON: {content[:200]!r}") from None
        data = json.loads(m.group(0))
    if not isinstance(data, dict):
        raise ValueError("LLM JSON reply is not an object")
    return data
