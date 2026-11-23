from __future__ import annotations

import json
import math
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from typing import Any

from .config import EmbeddingSettings


Transport = Callable[[str, dict[str, str], dict[str, Any], float], dict[str, Any]]


class EmbeddingError(RuntimeError):
    pass


class EmbeddingConfigurationError(EmbeddingError):
    pass


class EmbeddingResponseError(EmbeddingError):
    pass


class EmbeddingTransportError(EmbeddingError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def _default_transport(url: str, headers: dict[str, str], body: dict[str, Any], timeout: float) -> dict[str, Any]:
    request = urllib.request.Request(
        url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers=headers, method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            loaded = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise EmbeddingTransportError(f"embedding HTTP error: {exc.code}", status_code=exc.code) from None
    except urllib.error.URLError:
        raise EmbeddingTransportError("embedding network error") from None
    except TimeoutError:
        raise EmbeddingTransportError("embedding request timed out") from None
    except json.JSONDecodeError:
        raise EmbeddingResponseError("embedding response is not JSON") from None
    if not isinstance(loaded, dict):
        raise EmbeddingResponseError("embedding response must be a JSON object")
    return loaded


def _l2_normalize(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in vector))
    if not math.isfinite(norm) or norm <= 0:
        raise EmbeddingResponseError("embedding vector has invalid zero/non-finite norm")
    return [value / norm for value in vector]


class OpenAICompatibleEmbeddingClient:
    def __init__(
        self, settings: EmbeddingSettings, *, transport: Transport | None = None,
        timeout_seconds: float = 60.0,
    ) -> None:
        self.settings = settings
        self.model = settings.model
        self.dimension = settings.dimension
        self.embedding_version = settings.model
        self.transport = transport or _default_transport
        self.timeout_seconds = timeout_seconds
        self.calls = 0
        self.last_usage: dict[str, Any] = {}

    @property
    def endpoint(self) -> str:
        return self.settings.base_url.rstrip("/") + "/embeddings"

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        if any(not isinstance(text, str) or not text.strip() for text in texts):
            raise ValueError("embedding inputs must be non-empty strings")
        if not self.settings.api_key:
            raise EmbeddingConfigurationError("EMBEDDING_API_KEY is not configured")
        body = {"model": self.settings.model, "input": list(texts)}
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.settings.api_key}",
        }
        response = self.transport(self.endpoint, headers, body, self.timeout_seconds)
        self.calls += 1
        usage = response.get("usage")
        self.last_usage = dict(usage) if isinstance(usage, dict) else {}
        data = response.get("data")
        if not isinstance(data, list) or len(data) != len(texts):
            raise EmbeddingResponseError("embedding response count does not match input count")
        ordered = sorted(data, key=lambda item: int(item.get("index", 0)))
        vectors: list[list[float]] = []
        for item in ordered:
            vector = item.get("embedding")
            if not isinstance(vector, list) or len(vector) != self.dimension:
                raise EmbeddingResponseError(f"embedding dimension mismatch: expected {self.dimension}")
            try:
                numeric = [float(value) for value in vector]
            except (TypeError, ValueError):
                raise EmbeddingResponseError("embedding vector contains non-numeric values") from None
            vectors.append(_l2_normalize(numeric))
        return vectors

    def embed_one(self, text: str) -> list[float]:
        vectors = self.embed([text])
        if len(vectors) != 1:
            raise EmbeddingResponseError("single embedding request returned unexpected count")
        return vectors[0]
