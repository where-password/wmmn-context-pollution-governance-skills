from __future__ import annotations

import math

import pytest

from memory_core.config import EmbeddingSettings
from memory_core.embeddings import (
    EmbeddingConfigurationError,
    EmbeddingResponseError,
    OpenAICompatibleEmbeddingClient,
)


def test_embedding_settings_env_defaults_and_secret_repr(monkeypatch) -> None:
    monkeypatch.setenv("EMBEDDING_BASE_URL", "https://api.siliconflow.cn/v1")
    monkeypatch.setenv("EMBEDDING_API_KEY", "test-secret-value")
    monkeypatch.setenv("EMBEDDING_MODEL", "BAAI/bge-m3")
    monkeypatch.setenv("EMBEDDING_DIM", "1024")
    settings = EmbeddingSettings.from_env()
    assert settings.base_url == "https://api.siliconflow.cn/v1"
    assert settings.model == "BAAI/bge-m3"
    assert settings.dimension == 1024
    assert "test-secret-value" not in repr(settings)


def test_embedding_settings_load_from_env_file_without_revealing_value(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("EMBEDDING_API_KEY", "old-value")
    env_file = tmp_path / "embedding.env"
    env_file.write_text(
        "EMBEDDING_BASE_URL='https://api.siliconflow.cn/v1'\n"
        "EMBEDDING_API_KEY=fake-file-secret\n"
        "EMBEDDING_MODEL=BAAI/bge-m3\n"
        "EMBEDDING_DIM=1024\n",
        encoding="utf-8",
    )
    settings = EmbeddingSettings.from_env_file(env_file, override=True)
    assert settings.model == "BAAI/bge-m3"
    assert settings.dimension == 1024
    assert settings.api_key == "fake-file-secret"
    assert "fake-file-secret" not in repr(settings)


def test_openai_compatible_embedding_request_and_l2_normalization() -> None:
    captured = {}

    def transport(url, headers, body, timeout):
        captured.update({"url": url, "headers": headers, "body": body, "timeout": timeout})
        return {
            "data": [
                {"index": 1, "embedding": [3.0, 4.0, 0.0, 0.0]},
                {"index": 0, "embedding": [0.0, 5.0, 12.0, 0.0]},
            ],
            "usage": {"prompt_tokens": 8},
        }

    settings = EmbeddingSettings(
        base_url="https://api.siliconflow.cn/v1", api_key="test-secret-value",
        model="BAAI/bge-m3", dimension=4,
    )
    client = OpenAICompatibleEmbeddingClient(settings, transport=transport)
    vectors = client.embed(["今晚月色真美", "旧灯塔"])
    assert captured["url"] == "https://api.siliconflow.cn/v1/embeddings"
    assert captured["body"] == {"model": "BAAI/bge-m3", "input": ["今晚月色真美", "旧灯塔"]}
    assert captured["headers"]["Authorization"] == "Bearer test-secret-value"
    assert captured["timeout"] == 60.0
    assert client.last_usage == {"prompt_tokens": 8}
    assert vectors[0] == [0.0, 5.0 / 13.0, 12.0 / 13.0, 0.0]
    assert vectors[1][:2] == [0.6, 0.8]
    assert all(abs(math.sqrt(sum(x * x for x in vector)) - 1.0) < 1e-9 for vector in vectors)


def test_embedding_dimension_count_and_zero_vector_failures() -> None:
    settings = EmbeddingSettings(
        base_url="https://api.siliconflow.cn/v1", api_key="test-secret-value",
        model="BAAI/bge-m3", dimension=4,
    )
    dimension_client = OpenAICompatibleEmbeddingClient(
        settings, transport=lambda *args: {"data": [{"index": 0, "embedding": [1.0, 2.0]}]},
    )
    with pytest.raises(EmbeddingResponseError, match="dimension mismatch"):
        dimension_client.embed(["bad"])
    count_client = OpenAICompatibleEmbeddingClient(settings, transport=lambda *args: {"data": []})
    with pytest.raises(EmbeddingResponseError, match="count"):
        count_client.embed(["one"])
    zero_client = OpenAICompatibleEmbeddingClient(
        settings, transport=lambda *args: {"data": [{"index": 0, "embedding": [0.0, 0.0, 0.0, 0.0]}]},
    )
    with pytest.raises(EmbeddingResponseError, match="norm"):
        zero_client.embed(["zero"])


def test_embedding_client_requires_key_without_network_call() -> None:
    settings = EmbeddingSettings(
        base_url="https://api.siliconflow.cn/v1", api_key=None,
        model="BAAI/bge-m3", dimension=1024,
    )
    called = False

    def transport(*args):
        nonlocal called
        called = True
        return {}

    client = OpenAICompatibleEmbeddingClient(settings, transport=transport)
    with pytest.raises(EmbeddingConfigurationError, match="EMBEDDING_API_KEY"):
        client.embed_one("今晚月色真美")
    assert called is False
