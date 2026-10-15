"""Embedding providers for semantic memory retrieval.

- EmbeddingProvider ABC: embed(texts) -> list of float vectors.
- MockEmbeddings: deterministic hash-based vectors (tests, demos, offline).
  NOT semantically meaningful — only the geometry (cosine similarity) is real.
- OpenAIEmbeddings: any OpenAI-compatible /embeddings endpoint.

Cosine similarity lives here too, so recall code stays clean.
"""
from __future__ import annotations

import abc
import hashlib
import json
import math
import urllib.error
import urllib.request

from ..core.errors import ProviderError, ProviderUnavailable
from ..core.logging import get_logger

log = get_logger("memory.embeddings")


def cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


class EmbeddingProvider(abc.ABC):
    name = "base"
    dims: int = 0

    @abc.abstractmethod
    def embed(self, texts: list[str], timeout_s: float = 30.0) -> list[list[float]]:
        """Embed a batch of texts. Returns one vector per input, in order."""


class MockEmbeddings(EmbeddingProvider):
    """Deterministic pseudo-embeddings from token hashes.

    Each token contributes to hashed buckets; vectors are L2-normalized.
    Similar texts -> similar vectors (bag-of-words geometry). Good enough
    to test the retrieval machinery honestly; not a real semantic model.
    """
    name = "mock"
    dims = 128

    def embed(self, texts: list[str], timeout_s: float = 30.0) -> list[list[float]]:
        out = []
        for text in texts:
            vec = [0.0] * self.dims
            for token in text.lower().split():
                h = int(hashlib.sha256(token.encode()).hexdigest(), 16)
                vec[h % self.dims] += 1.0
                vec[(h >> 16) % self.dims] += 0.5
            norm = math.sqrt(sum(x * x for x in vec)) or 1.0
            out.append([x / norm for x in vec])
        return out


class OpenAIEmbeddings(EmbeddingProvider):
    name = "openai_embeddings"

    def __init__(self, base_url: str, model: str = "text-embedding-3-small",
                 api_key: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._api_key = api_key
        self.dims = 0  # discovered from first response

    def embed(self, texts: list[str], timeout_s: float = 30.0) -> list[list[float]]:
        if not texts:
            return []
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        req = urllib.request.Request(
            f"{self.base_url}/embeddings",
            data=json.dumps({"model": self.model, "input": texts}).encode(),
            headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                payload = json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            raise ProviderError(f"embeddings HTTP {e.code}") from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise ProviderUnavailable(f"embeddings unreachable: {e}") from e
        items = sorted(payload.get("data", []), key=lambda d: d["index"])
        vecs = [d["embedding"] for d in items]
        if vecs:
            self.dims = len(vecs[0])
        log.info("embedded %d texts (%d dims)", len(texts), self.dims)
        return vecs
