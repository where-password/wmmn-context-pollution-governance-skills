from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class EmbeddingSettings:
    base_url: str
    api_key: str | None = field(default=None, repr=False)
    model: str = "BAAI/bge-m3"
    dimension: int = 1024

    def __post_init__(self) -> None:
        if self.dimension <= 0:
            raise ValueError("embedding dimension must be positive")
        if not self.base_url.startswith(("http://", "https://")):
            raise ValueError("embedding base_url must be HTTP(S)")

    @classmethod
    def from_env_file(cls, path: str | Path, *, override: bool = False) -> "EmbeddingSettings":
        from .env import load_env_file

        load_env_file(path, override=override)
        return cls.from_env()

    @classmethod
    def from_env(cls) -> "EmbeddingSettings":
        return cls(
            base_url=os.environ.get("EMBEDDING_BASE_URL", "https://api.siliconflow.cn/v1"),
            api_key=os.environ.get("EMBEDDING_API_KEY"),
            model=os.environ.get("EMBEDDING_MODEL", "BAAI/bge-m3"),
            dimension=int(os.environ.get("EMBEDDING_DIM", "1024")),
        )


@dataclass(frozen=True)
class LLMSettings:
    base_url: str
    api_key: str | None = field(default=None, repr=False)
    model: str = "deepseek-chat"
    max_output_tokens: int = 4096

    def __post_init__(self) -> None:
        if not self.base_url.startswith(("http://", "https://")):
            raise ValueError("LLM base_url must be HTTP(S)")
        if self.max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be positive")

    @classmethod
    def from_env(cls) -> "LLMSettings":
        return cls(
            base_url=os.environ.get("LLM_BASE_URL", "https://api.deepseek.com"),
            api_key=os.environ.get("OPENAI_API_KEY"),
            model=os.environ.get("LLM_MODEL", "deepseek-chat"),
            max_output_tokens=int(os.environ.get("LLM_MAX_OUTPUT_TOKENS", "4096")),
        )
