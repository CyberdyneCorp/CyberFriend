"""Configuration loaded from the environment.

Every setting is provider-agnostic: the LLM base URL is configurable so the
same image runs against OpenAI, a LiteLLM proxy, or a self-hosted gateway.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


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
    #
    # NoDecode is required: without it pydantic-settings JSON-decodes complex
    # types straight from the environment, before any validator runs, so
    # `INDEXED_CHANNEL_IDS="100 200"` fails to parse rather than reaching the
    # splitter below.
    indexed_channel_ids: Annotated[frozenset[int], NoDecode] = frozenset()

    # --- Storage -------------------------------------------------------
    database_url: SecretStr

    # --- Embeddings ----------------------------------------------------
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: SecretStr
    embedding_model: str = "text-embedding-3-small"
    # Recorded here and in the schema: changing it is a reindex, not a swap.
    embedding_dimensions: int = 1536

    # --- Reasoning -----------------------------------------------------
    # The answering model: it judges evidence, plans and writes, so it is the
    # one that has to honour a response schema. What it provides is checked
    # against the stages the bot enables before the process serves anyone.
    chat_model: str = "gpt-4o"
    # The cheap handle on the same provider. Extraction and per-candidate
    # scoring run over traffic rather than over questions, so a frontier
    # model there is a standing bill nobody asked for.
    extraction_model: str = "gpt-4o-mini"
    # What the endpoint behind LLM_BASE_URL actually does, space- or
    # comma-separated. Empty means "whatever is known about this model name",
    # which for an unrecognised name is chat and nothing else: a self-hosted
    # stack must declare its own capabilities, because a model of the same
    # name served elsewhere may not behave the same way.
    #
    # NoDecode for the same reason as indexed_channel_ids: without it
    # pydantic-settings JSON-decodes the value before any validator runs.
    chat_model_capabilities: Annotated[frozenset[str], NoDecode] = frozenset()

    # --- Windowing -----------------------------------------------------
    # Guesses until measured against a real corpus; see design.md.
    window_max_messages: int = 10
    window_max_tokens: int = 512
    window_gap_seconds: int = 900

    # --- Serving -------------------------------------------------------
    health_port: int = 8080
    mcp_port: int = 8081

    @model_validator(mode="before")
    @classmethod
    def _blank_means_unset(cls, data: object) -> object:
        """Treat an empty environment variable as absent, not as a value.

        A deployment platform that renders every variable its compose file
        mentions writes a blank for each one nobody filled in, so the process
        sees WINDOW_MAX_MESSAGES="" rather than no such variable. Pydantic
        then tries to parse the blank and fails -- which turns every optional
        setting into a required one the moment it is exposed in a UI, and
        reports it as a malformed integer rather than as a missing value.

        Declaring a setting in the compose file must not make it mandatory.
        Required settings still fail, and now say "Field required", which
        names the actual problem.
        """
        if isinstance(data, dict):
            return {
                k: v
                for k, v in data.items()
                if not (isinstance(v, str) and not v.strip())
            }
        return data

    @field_validator("indexed_channel_ids", mode="before")
    @classmethod
    def _split_channel_ids(cls, v: object) -> object:
        if isinstance(v, str):
            return frozenset(int(p) for p in v.replace(",", " ").split())
        return v

    @field_validator("chat_model_capabilities", mode="before")
    @classmethod
    def _split_capabilities(cls, v: object) -> object:
        if isinstance(v, str):
            return frozenset(p for p in v.replace(",", " ").split())
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
