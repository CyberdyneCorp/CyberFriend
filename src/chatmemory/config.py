"""Configuration loaded from the environment.

Every setting is provider-agnostic: the LLM base URL is configurable so the
same image runs against OpenAI, a LiteLLM proxy, or a self-hosted gateway.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- Discord -------------------------------------------------------
    discord_token: SecretStr
    discord_guild_id: int

    # Channels to index. Empty means "none": indexing is opt-in, because the
    # corpus is a permanent record of what people said and defaulting it to
    # "everything the bot can see" is not a decision code should make.
    indexed_channel_ids: frozenset[int] = frozenset()

    # --- Storage -------------------------------------------------------
    database_url: SecretStr

    # --- Embeddings ----------------------------------------------------
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: SecretStr
    embedding_model: str = "text-embedding-3-small"
    # Recorded here and in the schema: changing it is a reindex, not a swap.
    embedding_dimensions: int = 1536

    # --- Windowing -----------------------------------------------------
    # Guesses until measured against a real corpus; see design.md.
    window_max_messages: int = 10
    window_max_tokens: int = 512
    window_gap_seconds: int = 900

    # --- Serving -------------------------------------------------------
    health_port: int = 8080
    mcp_port: int = 8081

    @field_validator("indexed_channel_ids", mode="before")
    @classmethod
    def _split_channel_ids(cls, v: object) -> object:
        if isinstance(v, str):
            return frozenset(int(p) for p in v.replace(",", " ").split())
        return v

    @field_validator("embedding_dimensions")
    @classmethod
    def _positive_dimensions(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("embedding_dimensions must be positive")
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
