"""Settings an operator may change without a redeploy, and what beats what.

Precedence is database, then environment, then default, and every resolved
value carries which of the three it came from. That provenance is not a
nicety: a setting edited in the console but overridden by an environment
variable looks exactly like a setting that never saved, and an operator who
cannot tell those apart edits the same box again instead of looking at the
deployment.

Three rules here are worth reading before changing anything:

*   **A failed refresh keeps the current configuration.** It does not fall
    back to the environment. Falling back reads as the safe choice and is the
    opposite of one: an operator who has just removed a channel from indexing
    scope, or revoked a tool, would have that narrowing silently undone by a
    database blip. Configuration changes are asymmetric -- the dangerous
    direction is widening -- so the safe default is to change nothing.

*   **A value that cannot be parsed keeps its previous value and is
    reported.** A running process that is answering questions must not be
    taken down by one bad row, and at startup "previous" is the environment,
    which is the same thing as starting with the environment configuration and
    saying what could not be applied.

*   **Credentials are not settings.** The platform token, the model key and
    the database URL are read from the environment and are not resolvable
    here at all, so a row someone inserts into `app_setting` under one of
    those names is ignored and reported rather than obeyed. They are also
    needed *to reach* the database, which makes storing them there circular.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Generic, TypeVar, cast

import structlog

from chatmemory.config import Settings
from chatmemory.ports.configuration import (
    ConfigurationStore,
    ResolvedValue,
    SettingSource,
    StoredSetting,
)

log = structlog.get_logger()

T = TypeVar("T")

# How often a long-running process re-reads stored configuration. The upper
# bound on how long a revoked tool stays callable or a de-scoped channel stays
# indexed, so it is short enough that an operator watching the console sees
# their change take hold, and long enough that eight processes re-reading a
# dozen rows is not a workload.
REFRESH_INTERVAL_SECONDS = 30.0


# --- failures -----------------------------------------------------------


class ConfigurationRefused(ValueError):
    """A change that will not be stored. The message is shown to the operator."""


class SecretSetting(ConfigurationRefused):
    """An attempt to store a credential."""


class UnknownSetting(ConfigurationRefused):
    """A key that is not a setting this system has."""


class MalformedSetting(ConfigurationRefused):
    """A value that does not parse as the setting's type."""


class MissingSecret(RuntimeError):
    """A required credential is absent from the environment."""


# --- parsing ------------------------------------------------------------
#
# Stored values are text, because that is what a form submits and what an
# audit row can hold verbatim. These mirror the validators in
# `chatmemory.config`, so that `INDEXED_CHANNEL_IDS="100 200"` and a stored
# "100 200" mean the same thing; a test asserts that parity rather than
# trusting this comment.
#
# None of these messages names the text it was given, and that is the point
# rather than a style. A refusal travels into `config_audit`, which migration
# 0012 makes append-only, and into the operator's browser; the likeliest bad
# value on this surface is a credential pasted into the wrong box. `int()` and
# `float()` quote their input ("could not convert string to float: '...'"), so
# the stdlib's message is exactly what must not escape -- hence the wrappers
# below, which raise `from None` so the original does not travel as context
# into a traceback either.

_TRUE = frozenset({"1", "true", "t", "yes", "y", "on"})
_FALSE = frozenset({"0", "false", "f", "no", "n", "off"})


def _words(raw: str) -> tuple[str, ...]:
    """Split on whitespace or commas, keeping the operator's order."""
    return tuple(part for part in raw.replace(",", " ").split())


def _whole(raw: str) -> int:
    """`int` with a message of our own; the stdlib's repeats what it was given."""
    try:
        return int(raw.strip())
    except ValueError:
        raise ValueError("expected a whole number") from None


def _decimal(raw: str) -> float:
    """`float` with a message of our own, for the same reason as `_whole`."""
    try:
        return float(raw.strip())
    except ValueError:
        raise ValueError("expected a number") from None


def _channel_ids(raw: str) -> frozenset[int]:
    return frozenset(_whole(part) for part in _words(raw))


def _flag(raw: str) -> bool:
    text = raw.strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    raise ValueError("expected a true/false value")


def _positive_int(raw: str) -> int:
    value = _whole(raw)
    if value <= 0:
        raise ValueError("must be positive")
    return value


def _positive_float(raw: str) -> float:
    value = _decimal(raw)
    if value <= 0:
        raise ValueError("must be positive")
    return value


def _probability(raw: str) -> float:
    value = _decimal(raw)
    if not 0.0 <= value <= 1.0:
        # Refused rather than clamped, as in `config`: a threshold of 2.0
        # reports nothing, and "no obligations" is indistinguishable from
        # "nobody asked me anything".
        raise ValueError("must be between 0 and 1")
    return value


@dataclass(frozen=True, slots=True)
class ValueType(Generic[T]):
    """How a setting's text is read, and the only wording a refusal may use.

    `expects` is carried beside the parser rather than written at each refusal
    so that the two cannot drift, and so that a refusal has something to say
    *instead of* the submitted value. Without it, "not quoting the value" would
    mean "malformed", which tells an operator nothing and sends them back to
    the same box to guess again.
    """

    read: Callable[[str], T]
    expects: str

    def __call__(self, raw: str) -> T:
        return self.read(raw)


CHANNEL_IDS: ValueType[frozenset[int]] = ValueType(
    _channel_ids, "channel ids, separated by spaces or commas"
)
WORDS: ValueType[tuple[str, ...]] = ValueType(_words, "entries separated by spaces or commas")
FLAG: ValueType[bool] = ValueType(_flag, "true or false")
POSITIVE_INT: ValueType[int] = ValueType(_positive_int, "a whole number greater than 0")
POSITIVE_FLOAT: ValueType[float] = ValueType(_positive_float, "a number greater than 0")
PROBABILITY: ValueType[float] = ValueType(_probability, "a number between 0 and 1")


# --- the settings -------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SettingSpec(Generic[T]):
    """One editable setting: its name, how to read its text, and why it exists.

    `key` is the field name on `Settings`, not a separate vocabulary. One name
    for one setting is what lets a value be explained -- the console, the
    environment variable and the log line all say `indexed_channel_ids`.
    """

    key: str
    parse: ValueType[T]
    summary: str

    @property
    def env_var(self) -> str:
        return self.key.upper()

    @property
    def expects(self) -> str:
        """What a refusal says the setting accepts, in place of what was sent."""
        return self.parse.expects


INDEXED_CHANNEL_IDS: SettingSpec[frozenset[int]] = SettingSpec(
    "indexed_channel_ids",
    CHANNEL_IDS,
    "channels whose messages are archived; removing one stops ingestion",
)
ASK_EXTRACTION_ENABLED: SettingSpec[bool] = SettingSpec(
    "ask_extraction_enabled", FLAG, "whether obligations are extracted from traffic"
)
ASK_MIN_CONFIDENCE: SettingSpec[float] = SettingSpec(
    "ask_min_confidence", PROBABILITY, "presentation threshold for an extracted ask"
)
ASK_STALE_AFTER_DAYS: SettingSpec[int] = SettingSpec(
    "ask_stale_after_days", POSITIVE_INT, "when an unanswered ask becomes stale"
)
ASK_EXTRACTION_WINDOW_MESSAGES: SettingSpec[int] = SettingSpec(
    "ask_extraction_window_messages", POSITIVE_INT, "messages per extraction batch"
)
FEDERATION_SERVERS: SettingSpec[tuple[str, ...]] = SettingSpec(
    "federation_servers", WORDS, "external MCP servers, as name=target"
)
FEDERATION_TOOL_ALLOWLIST: SettingSpec[tuple[str, ...]] = SettingSpec(
    "federation_tool_allowlist",
    WORDS,
    "tools an operator has made available; removing one revokes it",
)
FEDERATION_CREDENTIAL_HOLDERS: SettingSpec[tuple[str, ...]] = SettingSpec(
    "federation_credential_holders", WORDS, "who may spend a state-changing tool"
)
FEDERATION_MAX_TOOLS_PER_RUN: SettingSpec[int] = SettingSpec(
    "federation_max_tools_per_run", POSITIVE_INT, "federated tools offered to one run"
)
WEB_TOOLS_ENABLED: SettingSpec[bool] = SettingSpec(
    "web_tools_enabled", FLAG, "whether the agent may reach the public web"
)
WEB_MAX_CALLS_PER_RUN: SettingSpec[int] = SettingSpec(
    "web_max_calls_per_run", POSITIVE_INT, "outbound calls one question may cause"
)
WEB_TIMEOUT_SECONDS: SettingSpec[float] = SettingSpec(
    "web_timeout_seconds", POSITIVE_FLOAT, "how long one outbound call may take"
)
WINDOW_MAX_MESSAGES: SettingSpec[int] = SettingSpec(
    "window_max_messages", POSITIVE_INT, "messages per retrieval window"
)
WINDOW_MAX_TOKENS: SettingSpec[int] = SettingSpec(
    "window_max_tokens", POSITIVE_INT, "tokens per retrieval window"
)
WINDOW_GAP_SECONDS: SettingSpec[int] = SettingSpec(
    "window_gap_seconds", POSITIVE_INT, "silence that ends a retrieval window"
)

# Deliberately absent: `embedding_model` and `embedding_dimensions`. Changing
# either invalidates every stored vector, so it is a reindex rather than a
# setting, and a console checkbox is the wrong shape for that decision.
SETTINGS: Mapping[str, SettingSpec[Any]] = {
    spec.key: spec
    for spec in (
        INDEXED_CHANNEL_IDS,
        ASK_EXTRACTION_ENABLED,
        ASK_MIN_CONFIDENCE,
        ASK_STALE_AFTER_DAYS,
        ASK_EXTRACTION_WINDOW_MESSAGES,
        FEDERATION_SERVERS,
        FEDERATION_TOOL_ALLOWLIST,
        FEDERATION_CREDENTIAL_HOLDERS,
        FEDERATION_MAX_TOOLS_PER_RUN,
        WEB_TOOLS_ENABLED,
        WEB_MAX_CALLS_PER_RUN,
        WEB_TIMEOUT_SECONDS,
        WINDOW_MAX_MESSAGES,
        WINDOW_MAX_TOKENS,
        WINDOW_GAP_SECONDS,
    )
}


@dataclass(frozen=True, slots=True)
class SecretSpec:
    """A credential: environment-only, never stored, never displayed."""

    key: str
    summary: str
    required: bool

    @property
    def env_var(self) -> str:
        return self.key.upper()


SECRETS: Mapping[str, SecretSpec] = {
    secret.key: secret
    for secret in (
        SecretSpec("discord_token", "the bot's platform token", required=True),
        SecretSpec("llm_api_key", "the model API key", required=True),
        SecretSpec("database_url", "the database URL", required=True),
        # Optional, and still a credential: absent means Google is not
        # registered at all, which is a configuration decision, not a secret
        # that may be kept somewhere more convenient.
        SecretSpec("serpapi_key", "the web search API key", required=False),
    )
}


def check_required_secrets(environ: Mapping[str, str]) -> None:
    """Fail loudly, naming the setting, when a required credential is absent.

    A process that starts without its model key fails later, once, inside
    whichever job first needed it -- which reads as an outage rather than as a
    missing variable. Raised before anything else is built so the name of the
    setting is the first thing in the log.
    """
    missing = [
        secret
        for secret in SECRETS.values()
        if secret.required and not environ.get(secret.env_var, "").strip()
    ]
    if missing:
        named = ", ".join(f"{s.key} ({s.env_var}: {s.summary})" for s in missing)
        raise MissingSecret(f"required credentials are absent from the environment: {named}")


# --- resolved configuration ---------------------------------------------


@dataclass(frozen=True, slots=True)
class SettingView:
    """One setting as an operator should see it: the value and its provenance."""

    key: str
    value: object
    source: SettingSource
    summary: str
    updated_by: str | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ConfigurationSnapshot:
    """Every setting in force at one instant, with where each came from.

    Immutable, and replaced wholesale on refresh rather than mutated in place.
    A consumer that read a value keeps reading a consistent world for the rest
    of its pass instead of seeing half of a change.
    """

    values: Mapping[str, ResolvedValue]

    def get(self, spec: SettingSpec[T]) -> T:
        """The value in force, typed by the spec that describes it."""
        return cast(T, self.values[spec.key].value)

    def source(self, spec: SettingSpec[Any]) -> SettingSource:
        return self.values[spec.key].source

    def explain(self) -> tuple[SettingView, ...]:
        """Every setting, for a surface that has to show why a value is what it is."""
        return tuple(
            SettingView(
                key=key,
                value=self.values[key].value,
                source=self.values[key].source,
                summary=spec.summary,
                updated_by=self.values[key].updated_by,
                updated_at=self.values[key].updated_at,
            )
            for key, spec in sorted(SETTINGS.items())
            if key in self.values
        )


@dataclass(frozen=True, slots=True)
class ConfigurationProblem:
    """A stored row that was not applied, and why. Reported, never raised."""

    key: str
    kind: str
    detail: str


@dataclass(frozen=True, slots=True)
class RefreshReport:
    """What one refresh did, for a health surface and for tests.

    `applied` is False when stored configuration could not be read at all. The
    distinction is the whole point of the failure rule: nothing changed, and
    that is not the same as nothing being stored.
    """

    applied: bool
    changed: tuple[str, ...] = ()
    problems: tuple[ConfigurationProblem, ...] = ()


def environment_snapshot(
    settings: Settings, environ: Mapping[str, str]
) -> dict[str, ResolvedValue]:
    """The baseline: what the process was started with, and whether it was chosen.

    `DEFAULT` means nobody said anything; `ENVIRONMENT` means somebody did.
    The distinction is drawn from the variable being present, and from the
    value differing from the field's default -- the second clause catches a
    value set in a `.env` file, which pydantic reads and `os.environ` does
    not show.
    """
    resolved: dict[str, ResolvedValue] = {}
    for key, spec in SETTINGS.items():
        value = getattr(settings, key)
        resolved[key] = ResolvedValue(value, _environment_source(spec, value, environ))
    return resolved


def _environment_source(
    spec: SettingSpec[Any], value: object, environ: Mapping[str, str]
) -> SettingSource:
    if environ.get(spec.env_var, "").strip():
        return SettingSource.ENVIRONMENT
    default = Settings.model_fields[spec.key].get_default(call_default_factory=True)
    return SettingSource.DEFAULT if value == default else SettingSource.ENVIRONMENT


class RuntimeConfiguration:
    """The settings in force, re-read from the database on a bounded cadence.

    Holds one snapshot and swaps it; consumers either read `current` each time
    they act, or register with `on_change` and narrow what they are doing. A
    consumer that captured a value at construction is a consumer that will
    still be indexing a channel an operator removed an hour ago.
    """

    def __init__(
        self,
        store: ConfigurationStore,
        environment: Mapping[str, ResolvedValue],
        *,
        specs: Mapping[str, SettingSpec[Any]] = SETTINGS,
    ) -> None:
        self._store = store
        self._specs = specs
        # Kept separately from the snapshot: every refresh rebuilds on top of
        # this, so clearing a stored setting hands the setting back to the
        # environment instead of leaving the last stored value in force.
        self._environment = dict(environment)
        self._current = ConfigurationSnapshot(dict(environment))
        self._observers: list[Callable[[ConfigurationSnapshot], None]] = []

    @classmethod
    def from_settings(
        cls,
        store: ConfigurationStore,
        settings: Settings,
        environ: Mapping[str, str],
    ) -> RuntimeConfiguration:
        return cls(store, environment_snapshot(settings, environ))

    @property
    def current(self) -> ConfigurationSnapshot:
        return self._current

    def on_change(self, observer: Callable[[ConfigurationSnapshot], None]) -> None:
        """Register something that has to act on a change rather than poll for it."""
        self._observers.append(observer)

    async def refresh(self) -> RefreshReport:
        """Re-read stored configuration. Never raises.

        A refresh runs in a loop that must outlive every failure it can meet:
        a loop that dies on an unreachable database leaves a process running
        with configuration frozen at boot and no indication that it is.
        """
        try:
            stored = await self._store.load()
        except Exception as exc:  # noqa: BLE001 - any failure means "keep what we have"
            # Explicitly NOT a fallback to the environment. See the module
            # docstring: reverting to the environment here would undo a
            # narrowing an operator has already made.
            log.error(
                "config.refresh_failed",
                error=type(exc).__name__,
                kept=len(self._current.values),
                detail="keeping the configuration already in force",
            )
            return RefreshReport(applied=False)

        snapshot, problems = self._resolve(stored)
        changed = tuple(
            key
            for key, resolved in snapshot.values.items()
            if resolved.value != self._current.values[key].value
        )
        self._current = snapshot
        for problem in problems:
            log.warning(
                f"config.{problem.kind}", setting=problem.key, detail=problem.detail
            )
        if changed:
            log.info("config.changed", settings=sorted(changed))
            self._notify(snapshot)
        return RefreshReport(applied=True, changed=tuple(sorted(changed)), problems=problems)

    async def run_forever(self, interval: float = REFRESH_INTERVAL_SECONDS) -> None:
        """Refresh on a cadence, for as long as the process runs.

        Refreshes before its first sleep: the moment a process starts is when
        it knows least about what an operator has configured since the last
        deploy.
        """
        while True:
            await self.refresh()
            await asyncio.sleep(interval)

    def _resolve(
        self, stored: Sequence[StoredSetting]
    ) -> tuple[ConfigurationSnapshot, tuple[ConfigurationProblem, ...]]:
        values = dict(self._environment)
        problems: list[ConfigurationProblem] = []
        for setting in stored:
            problem = self._apply(setting, values)
            if problem is not None:
                problems.append(problem)
        return ConfigurationSnapshot(values), tuple(problems)

    def _apply(
        self, setting: StoredSetting, values: dict[str, ResolvedValue]
    ) -> ConfigurationProblem | None:
        """Fold one stored row into the snapshot being built, or explain why not."""
        if setting.key in SECRETS:
            # Reachable only by writing to the table directly -- the editor
            # refuses it -- which is exactly why it is checked again here.
            return ConfigurationProblem(
                setting.key, "secret_ignored", "credentials are read from the environment only"
            )
        spec = self._specs.get(setting.key)
        if spec is None:
            return ConfigurationProblem(
                setting.key, "unknown_setting", "no setting by that name; row ignored"
            )
        try:
            parsed = spec.parse(setting.raw)
        except (ValueError, TypeError):
            # The previous value, not the environment value: a malformed edit
            # must not revert an earlier good one.
            values[setting.key] = self._current.values[setting.key]
            # A row written straight into the table is as likely to hold a
            # pasted credential as a form submission is, and this detail is
            # logged and handed to a health surface. Same rule as the editor:
            # what it accepts, not what it was given.
            return ConfigurationProblem(
                setting.key, "value_rejected", f"expected {spec.expects}"
            )
        values[setting.key] = ResolvedValue(
            parsed, SettingSource.DATABASE, setting.updated_by, setting.updated_at
        )
        return None

    def _notify(self, snapshot: ConfigurationSnapshot) -> None:
        for observer in self._observers:
            try:
                observer(snapshot)
            except Exception:
                # One consumer that cannot cope with a change must not stop
                # the others from seeing it, nor end the refresh loop.
                log.exception("config.observer_failed", observer=repr(observer))


# --- editing ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ConfigurationEditor:
    """The only supported way to change stored configuration.

    Validation happens here rather than at read time so that a bad value is
    refused while an operator is looking at the screen, instead of being
    reported into a log an hour later by a process that kept its old value.
    """

    store: ConfigurationStore
    # A factory because a mapping is not a hashable default; the value is
    # the same registry every caller resolves against.
    specs: Mapping[str, SettingSpec[Any]] = field(default_factory=lambda: SETTINGS)

    async def set(self, key: str, raw: str, operator: str) -> object:
        """Store a setting, returning the parsed value. Raises on refusal."""
        spec = await self._require_editable(key, operator)
        try:
            parsed = spec.parse(raw)
        except (ValueError, TypeError) as exc:
            # The spec's own wording, never `str(exc)`. A parser is free to be
            # careless with the text it was handed -- the stdlib's number
            # parsers quote it outright -- and this line is where that text
            # would become a permanent `config_audit` row and a 400 body in an
            # operator's browser. Saying what would have been accepted is the
            # useful half of that message anyway.
            reason = f"malformed value; expected {spec.expects}"
            await self.store.record_refusal(key, operator, reason)
            raise MalformedSetting(f"{key}: {reason}") from exc
        await self.store.put(key, raw, operator)
        log.info("config.set", setting=key, operator=operator)
        return parsed

    async def clear(self, key: str, operator: str) -> None:
        """Remove a stored setting, handing it back to the environment."""
        await self._require_editable(key, operator)
        await self.store.clear(key, operator)
        log.info("config.cleared", setting=key, operator=operator)

    async def _require_editable(self, key: str, operator: str) -> SettingSpec[Any]:
        secret = SECRETS.get(key)
        if secret is not None:
            reason = (
                f"{key} is a credential ({secret.summary}); credentials are read from "
                "the environment and cannot be stored"
            )
            await self.store.record_refusal(key, operator, reason)
            raise SecretSetting(reason)
        spec = self.specs.get(key)
        if spec is None:
            reason = f"{key} is not a setting"
            await self.store.record_refusal(key, operator, reason)
            raise UnknownSetting(reason)
        return spec


def editable_keys() -> Iterable[str]:
    """The settings a console may offer, in the order it should list them."""
    return sorted(SETTINGS)
