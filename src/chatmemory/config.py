"""Configuration loaded from the environment.

Every setting is provider-agnostic: the LLM base URL is configurable so the
same image runs against OpenAI, a LiteLLM proxy, or a self-hosted gateway.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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

    # --- Asks ----------------------------------------------------------
    # Extraction is a standing cost proportional to server traffic rather
    # than to usage, so an operator has to be able to stop paying it without
    # redeploying a different image. Default on: a deployment that wired
    # extraction and then ran with it disabled by default would be the same
    # feature-that-never-runs defect this exists to close.
    ask_extraction_enabled: bool = True

    # Presentation threshold, not storage and not authorization. A
    # sub-threshold extraction is still recorded -- that is how the threshold
    # gets tuned against reality -- it simply never reaches an answer. Raising
    # it trades recall for precision, which is the direction this feature
    # errs: a false "you need to do X" costs trust in every other entry.
    ask_min_confidence: float = 0.6

    # When an unanswered ask becomes stale. Stale, never closed: an ask nobody
    # answered in three weeks is exactly what somebody asking "what do I need
    # to do" wants to see.
    ask_stale_after_days: int = 21

    # Messages buffered per channel before a batch is handed to extraction.
    # Roughly a window's worth: enough preceding conversation for the model to
    # tell a follow-up from an opener.
    ask_extraction_window_messages: int = 20

    # --- Notifications --------------------------------------------------
    # The assistant messaging somebody who did not ask it anything. Every
    # other bound below is a number; this one is the switch, and it is here
    # so an operator can stop the whole behaviour without redeploying a
    # different image. Default on, because a feature that ships disabled by
    # default is the feature-that-never-runs defect in a different costume.
    notifications_enabled: bool = True

    # How long an obligation waits for the others that will share its
    # message. The whole point of batching: a busy morning is one direct
    # message rather than nine.
    notification_batch_window_seconds: int = 300

    # The rate. At most one message per person per interval, enforced as a
    # predicate on the claim: reaching it makes pending notifications wait,
    # never dropped.
    notification_min_interval_seconds: int = 3600

    # No notification is ever queued for an obligation older than this. Not a
    # comfort setting: the backlog extraction pass reads a year of imported
    # history, and without a floor the first sweep after a deploy would
    # message everybody about everything they were ever asked.
    notification_max_age_hours: int = 24

    # A queued notification nobody could be sent by now is no longer news.
    # Settled unsent rather than delivered late.
    notification_expire_hours: int = 72

    # Obligations named in one message. The rest are counted, not listed.
    notification_max_items: int = 10

    # --- Conversation memory -------------------------------------------
    # How many of a person's most recent turns in one place are put in front
    # of the model verbatim. Each is a question and the answer it got, fenced
    # as data; more of them resolve older references and cost prompt on every
    # follow-up.
    memory_recent_turns: int = 6

    # Once a conversation holds more turns than this, the older ones are
    # condensed into a summary on EXTRACTION_MODEL, in the background. Must
    # exceed MEMORY_RECENT_TURNS, or there is nothing older to condense.
    memory_summarise_after_turns: int = 12

    # Remembered turns and summaries older than this are deleted by the ingest
    # process's sweep. What a person asked the assistant is personal data in
    # its own right, so it expires rather than accumulating.
    memory_retention_days: int = 30

    # --- Federation (optional) -----------------------------------------
    # External MCP servers CyberFriend may reach, as `name=target` entries,
    # space- or comma-separated: "issues=https://issues.internal/mcp". Empty
    # means "no federation", which is the correct default -- a deployment
    # that has not thought about external credentials must not acquire an
    # outbound channel by accident.
    #
    # NoDecode for the same reason as indexed_channel_ids: without it
    # pydantic-settings JSON-decodes the value before any validator runs.
    federation_servers: Annotated[tuple[str, ...], NoDecode] = ()

    # The tools an operator has explicitly made available, as `server:tool`,
    # optionally suffixed `:ro` to declare a tool read-only or
    # `:enable-mutation` to declare it state-changing and allow it to be
    # called at all. Discovery confers nothing: a server advertising a tool
    # that is not listed here stays unavailable, and a server's own claim to
    # be read-only is never honoured -- only the suffix here is. Undeclared
    # effect counts as mutating and is not enabled, so a tool listed without
    # a suffix is refused rather than called.
    federation_tool_allowlist: Annotated[tuple[str, ...], NoDecode] = ()

    # Who may spend a state-changing tool, as `server=platform:user_id`
    # entries: "issues=discord:1001". Required before `:enable-mutation`
    # means anything -- a mutating call carries the requester's own
    # authority, and this is where an operator says whose. Two variables
    # rather than one so that no single mistyped setting can give the agent
    # the ability to change something; empty means no one, which is what a
    # deployment that has not thought about this should get.
    federation_credential_holders: Annotated[tuple[str, ...], NoDecode] = ()

    # How many federated tools one run may be shown. A cap rather than
    # "all of them": a tool that was never offered is one the invoke-time
    # check refuses outright, so this bounds what a steered run can name.
    federation_max_tools_per_run: int = 5

    web_tools_enabled: bool = False
    """Wikipedia, and Google when a SerpApi key is set.

    Off by default, deliberately. This is the switch that gives the agent an
    outbound network connection, and it holds private channel content while
    ingesting text anyone in the server can write -- a deployment should
    acquire that boundary because someone chose it, not because they failed
    to turn it off. Wikipedia needs no credential, which makes it easy to
    enable and no less of a boundary.
    """

    serpapi_key: SecretStr | None = None
    """Absent means Google is not registered at all.

    Not registered rather than registered-and-failing: a tool the model can
    see but cannot use is one it will plan around and then fail on.
    """

    web_max_calls_per_run: int = 3
    """Outbound calls one question may cause, across all providers."""

    web_timeout_seconds: float = 10.0

    market_tools_enabled: bool = False
    """BTC/ETH prices, currency conversion, and the S&P 500 with a SerpApi key.

    Off by default for the same reason as `web_tools_enabled`: it is an
    outbound connection a chat message can trigger, and a deployment should
    acquire one because someone chose it. What leaves is narrower than a web
    query -- members of closed vocabularies checked in `app.egress` -- but
    "narrow" is not "none", and the switch is what makes it a decision.
    The S&P 500 reuses SERPAPI_KEY rather than a second key.
    """

    market_max_calls_per_run: int = 3
    """Market lookups one question may cause, across all market providers."""

    market_timeout_seconds: float = 8.0

    wallet_tools_enabled: bool = False
    """Wallet balances, Uniswap liquidity and Aave positions on Ethereum, Base
    and Arbitrum, given an address.

    Off by default like every other outbound boundary. What leaves is narrower
    than a web query -- an address, and only one the asker typed into their own
    question -- but "narrow" is not "none": which addresses this deployment is
    asked about is itself a signal, and it leaves in a request to a third party.
    """

    infura_key: SecretStr | None = None
    """Absent means the wallet tool is not registered at all.

    Not registered rather than registered-and-failing, for the same reason as
    `serpapi_key`: a tool the model can see but cannot use is one it will plan
    around and then fail on.
    """

    wallet_max_calls_per_run: int = 2
    """Wallet lookups one question may cause. Each reads every chain."""

    wallet_timeout_seconds: float = 8.0

    positions_timeout_seconds: float = 25.0
    """Per chain, for liquidity and Aave positions. Longer than a balance: a
    wallet with a hundred position NFTs is several multicalls."""

    # --- Scheduled tasks -----------------------------------------------
    scheduled_tasks_enabled: bool = False
    """Questions the assistant asks on a person's behalf, hourly to daily.

    Off by default, and this one is not an outbound boundary but an *inbound*
    one: it is the second feature that sends somebody a message they did not
    just ask for, and it is far more open-ended than the first. Every run is
    also a full reasoning run, so `people x tasks x 24` is the daily ceiling a
    deployment is signing up for.
    """

    scheduled_tasks_per_person: int = 5
    """How many a person may keep. The cap, with the hour floor, is what
    bounds the cost; neither is a budget."""

    scheduled_sweep_seconds: float = 300.0
    """How often due tasks are looked for. Well under the hour floor, so a
    task runs near its time rather than up to an interval late."""

    # --- Position alerts -----------------------------------------------
    alerts_enabled: bool = False
    """Direct messages when a watched Uniswap position leaves its range or an
    Aave health factor falls below a limit.

    Off by default, for both reasons the other switches give. It is an
    outbound boundary: every sweep sends the watched addresses to Infura with
    nobody asking, which is why an address is cleared once, when the alert is
    made. And it is an inbound one: the third feature that messages somebody
    unprompted. Needs `infura_key`; without one nothing is built.
    """

    alert_sweep_seconds: float = 300.0
    """How often alerts are checked. Every alert is read each sweep, in one
    multicall per chain, so this is the whole cost knob. Not below 60."""

    # --- Tracing -------------------------------------------------------
    tracing_enabled: bool = False
    """Export each run -- question, answer and retrieved evidence -- for study.

    Off by default, and not only for the usual outbound-boundary reason. What
    leaves here is not a query rooted in the asker's words, as with the web
    and market tools; it is the retrieved content itself. The destination
    therefore holds private-channel text with no viewer scoping, and access to
    it has to be restricted the way access to the database is. That is a
    decision an operator makes knowingly or not at all.
    """

    langfuse_host: str = ""
    """Absent means tracing is not configured, whatever `tracing_enabled` says."""

    langfuse_public_key: SecretStr | None = None
    langfuse_secret_key: SecretStr | None = None

    tracing_timeout_seconds: float = 5.0
    """What an export may cost before it is abandoned.

    Bounded rather than generous: the export happens after the answer is
    written but before the reply returns, so this is time a person spends
    waiting for something that is not for them.
    """

    # --- Voice questions -----------------------------------------------
    voice_questions_enabled: bool = False
    """A voice message sent to the bot in a DM, answered as if it were typed.

    Off by default, and an outbound boundary of a new kind: what leaves is a
    recording of somebody's voice, sent to the transcription endpoint below.
    Only the asker's own message in their own DM is ever sent, never anything
    said in a channel. Off, a voice message is answered once that voice is not
    enabled here, and nothing is downloaded.
    """

    voice_max_seconds: int = 120
    """The longest voice question accepted.

    Checked against any duration the client declares before download, and
    against the length counted from the audio's own packets after it, before
    anything is sent. A file that declares none is charged this much."""

    voice_max_bytes: int = 10_000_000
    """The largest download, enforced on the bytes as they arrive."""

    voice_person_monthly_minutes: int = 60
    """Minutes of voice questions one person may send in a calendar month (UTC)."""

    media_audio_monthly_minutes: int = 1500
    """Minutes of audio transcribed in a calendar month (UTC), for everybody.

    The hard ceiling on the transcription bill: audio longer than it was
    charged is refused before it is sent. Shared with any later transcription
    of channel voice notes, so it bounds the whole feature."""

    media_base_url: str = "https://api.openai.com/v1"
    """The OpenAI-compatible endpoint audio is sent to: `{this}/audio/transcriptions`.

    Its own setting rather than LLM_BASE_URL, because where people's voices go
    is a separate decision from where their text goes."""

    media_api_key: SecretStr | None = None
    """Required once VOICE_QUESTIONS_ENABLED is on; refused at boot without it."""

    media_audio_model: str = "gpt-4o-mini-transcribe"

    media_timeout_seconds: float = 30.0
    """What one download, and separately one transcription, may take."""

    # --- Time ----------------------------------------------------------
    answer_timezone: str = "America/Sao_Paulo"
    """The zone whose calendar a question's "ontem" or "last week" means.

    An IANA name. Where a day starts is a property of the deployment, not of
    UTC: cut at UTC midnight, "yesterday" asked at 22:00 in Sao Paulo is the
    day before the one meant. Nothing reads it yet: the said-by route (PR 3)
    will pass it into `app.timespan.parse_span`, which takes the zone as an
    argument and never reads settings itself. The prompt's clock notice and
    the older period tables still speak UTC.
    """

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

    @field_validator(
        "federation_servers",
        "federation_tool_allowlist",
        "federation_credential_holders",
        mode="before",
    )
    @classmethod
    def _split_federation_entries(cls, v: object) -> object:
        """Split on whitespace or commas, keeping the operator's order.

        Order is kept so startup logs list servers the way the operator wrote
        them; a set would reorder the one output they read to check their
        configuration took effect.
        """
        if isinstance(v, str):
            return tuple(p for p in v.replace(",", " ").split())
        return v

    @field_validator("answer_timezone")
    @classmethod
    def _known_timezone(cls, v: str) -> str:
        """Refused at boot: an unknown zone would otherwise fail on the first
        question that names a day, as an error nobody links to a setting."""
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"answer_timezone {v!r} is not a known IANA zone") from exc
        return v

    @field_validator("embedding_dimensions")
    @classmethod
    def _positive_dimensions(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("embedding_dimensions must be positive")
        return v

    @field_validator("ask_min_confidence")
    @classmethod
    def _confidence_is_a_probability(cls, v: float) -> float:
        """Refused rather than clamped.

        A threshold of 2.0 silently reports nothing, and "the obligations
        feature returns nothing" is indistinguishable from "nobody asked me
        anything" -- which is the failure this project keeps shipping.
        """
        if not 0.0 <= v <= 1.0:
            raise ValueError("ask_min_confidence must be between 0 and 1")
        return v

    @field_validator(
        "memory_recent_turns", "memory_summarise_after_turns", "memory_retention_days"
    )
    @classmethod
    def _positive_memory_setting(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("must be positive")
        return v

    @model_validator(mode="after")
    def _summary_bound_exceeds_recent_turns(self) -> Settings:
        """Refused at boot rather than at the first summary.

        A bound at or below the verbatim window condenses nothing, and would
        otherwise surface as a background error on somebody's tenth question.
        """
        if self.memory_summarise_after_turns <= self.memory_recent_turns:
            raise ValueError(
                "memory_summarise_after_turns must exceed memory_recent_turns"
            )
        return self

    @field_validator("market_max_calls_per_run", "market_timeout_seconds")
    @classmethod
    def _positive_market_setting(cls, v: float) -> float:
        """Refused at boot: a zero budget registers tools that refuse every call."""
        if v <= 0:
            raise ValueError("must be positive")
        return v

    @field_validator(
        "notification_batch_window_seconds",
        "notification_min_interval_seconds",
        "notification_max_age_hours",
        "notification_expire_hours",
        "notification_max_items",
    )
    @classmethod
    def _positive_notification_setting(cls, v: int) -> int:
        """Refused at boot rather than silently meaning "no bound".

        A zero batching window sends a message per obligation, and a zero
        rate interval removes the rate limit entirely -- both of which turn
        the feature into the thing it was bounded to avoid being.
        """
        if v <= 0:
            raise ValueError("must be positive")
        return v

    @field_validator("alert_sweep_seconds")
    @classmethod
    def _alert_sweep_floor(cls, v: float) -> float:
        """Refused at boot: a sweep every few seconds is a steady stream of
        requests to Infura on behalf of people who are not asking anything."""
        if v < 60:
            raise ValueError("must be at least 60 seconds")
        return v

    @field_validator(
        "voice_max_seconds",
        "voice_max_bytes",
        "voice_person_monthly_minutes",
        "media_audio_monthly_minutes",
        "media_timeout_seconds",
    )
    @classmethod
    def _positive_voice_setting(cls, v: float) -> float:
        """Refused at boot: a zero cap refuses every voice question, and reads
        to an operator as a feature that is on."""
        if v <= 0:
            raise ValueError("must be positive")
        return v

    @field_validator("media_base_url")
    @classmethod
    def _http_media_endpoint(cls, v: str) -> str:
        if not v.lower().startswith(("https://", "http://")):
            raise ValueError("media_base_url must be an http(s) URL")
        return v.rstrip("/")

    @model_validator(mode="after")
    def _voice_needs_a_key(self) -> Settings:
        """Refused at boot rather than on somebody's first voice message, where
        it would read as "I couldn't understand the audio"."""
        if self.voice_questions_enabled and self.media_api_key is None:
            raise ValueError("voice_questions_enabled needs media_api_key")
        return self

    @field_validator("ask_stale_after_days", "ask_extraction_window_messages")
    @classmethod
    def _positive_ask_setting(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("must be positive")
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
