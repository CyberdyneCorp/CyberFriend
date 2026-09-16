"""Stored configuration: precedence, provenance, and what a failure must not do.

The rule this file exists for is the refresh-failure rule. Falling back to the
environment when the database cannot be read looks like the cautious choice and
is the opposite of one: it silently undoes a narrowing an operator has already
made -- a channel taken out of indexing scope, a tool revoked -- and it does so
at exactly the moment nobody is watching. Several tests below assert the
narrowing survives rather than asserting an error was logged.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime

import pytest

from chatmemory.app.configuration import (
    ASK_MIN_CONFIDENCE,
    FEDERATION_TOOL_ALLOWLIST,
    INDEXED_CHANNEL_IDS,
    SECRETS,
    SETTINGS,
    WEB_TOOLS_ENABLED,
    ConfigurationEditor,
    MalformedSetting,
    MissingSecret,
    RuntimeConfiguration,
    SecretSetting,
    UnknownSetting,
    check_required_secrets,
    environment_snapshot,
)
from chatmemory.config import Settings
from chatmemory.ports.configuration import SettingSource, StoredSetting

TOKEN = "zzz-discord-bot-token-zzz"
BASE = {
    "discord_token": TOKEN,
    "discord_guild_id": 1,
    "database_url": "postgresql+asyncpg://u:p@h/d",
    "llm_api_key": "k",
}
LIVE_ENVIRON = {
    "DISCORD_TOKEN": TOKEN,
    "DISCORD_GUILD_ID": "1",
    "DATABASE_URL": "postgresql+asyncpg://u:p@h/d",
    "LLM_API_KEY": "k",
}

AT = datetime(2026, 1, 1, tzinfo=UTC)


def settings(**overrides: object) -> Settings:
    return Settings(**{**BASE, **overrides})  # type: ignore[arg-type]


def stored(key: str, raw: str, by: str = "ana") -> StoredSetting:
    return StoredSetting(key=key, raw=raw, updated_by=by, updated_at=AT)


class FakeConfigurationStore:
    """A `ConfigurationStore` that can be made to fail on demand."""

    def __init__(self, rows: Sequence[StoredSetting] = ()) -> None:
        self.rows = list(rows)
        self.fail_with: Exception | None = None
        self.puts: list[tuple[str, str, str]] = []
        self.cleared: list[tuple[str, str]] = []
        self.refusals: list[tuple[str, str, str]] = []
        self.loads = 0

    async def load(self) -> Sequence[StoredSetting]:
        self.loads += 1
        if self.fail_with is not None:
            raise self.fail_with
        return list(self.rows)

    async def put(self, key: str, raw: str, operator: str) -> None:
        self.puts.append((key, raw, operator))
        self.rows = [row for row in self.rows if row.key != key]
        self.rows.append(stored(key, raw, operator))

    async def clear(self, key: str, operator: str) -> None:
        self.cleared.append((key, operator))
        self.rows = [row for row in self.rows if row.key != key]

    async def record_refusal(self, key: str, operator: str, reason: str) -> None:
        self.refusals.append((key, operator, reason))


def configuration(
    store: FakeConfigurationStore, *, environ: dict[str, str] | None = None, **overrides: object
) -> RuntimeConfiguration:
    return RuntimeConfiguration.from_settings(
        store, settings(**overrides), environ if environ is not None else dict(LIVE_ENVIRON)
    )


# --- precedence ---------------------------------------------------------


async def test_a_stored_setting_is_used_in_place_of_the_environment() -> None:
    store = FakeConfigurationStore([stored("indexed_channel_ids", "300 400")])
    config = configuration(store, indexed_channel_ids="100 200")

    await config.refresh()

    assert config.current.get(INDEXED_CHANNEL_IDS) == {300, 400}
    assert config.current.source(INDEXED_CHANNEL_IDS) is SettingSource.DATABASE


async def test_an_unstored_setting_falls_back_to_the_environment() -> None:
    store = FakeConfigurationStore()
    config = configuration(
        store,
        environ={**LIVE_ENVIRON, "INDEXED_CHANNEL_IDS": "100 200"},
        indexed_channel_ids="100 200",
    )

    await config.refresh()

    assert config.current.get(INDEXED_CHANNEL_IDS) == {100, 200}
    assert config.current.source(INDEXED_CHANNEL_IDS) is SettingSource.ENVIRONMENT


async def test_a_setting_nobody_configured_reports_a_default() -> None:
    config = configuration(FakeConfigurationStore())

    await config.refresh()

    assert config.current.get(ASK_MIN_CONFIDENCE) == 0.6
    assert config.current.source(ASK_MIN_CONFIDENCE) is SettingSource.DEFAULT


async def test_a_value_from_a_dotenv_file_is_not_reported_as_a_default() -> None:
    """pydantic reads `.env`; `os.environ` does not show it.

    Reporting such a value as DEFAULT would tell an operator nobody had
    configured it, which is the one thing provenance exists to prevent.
    """
    config = configuration(FakeConfigurationStore(), web_tools_enabled=True)

    await config.refresh()

    assert config.current.source(WEB_TOOLS_ENABLED) is SettingSource.ENVIRONMENT


async def test_clearing_a_stored_setting_hands_it_back_to_the_environment() -> None:
    store = FakeConfigurationStore([stored("web_max_calls_per_run", "9")])
    config = configuration(store)
    await config.refresh()
    assert config.current.values["web_max_calls_per_run"].value == 9

    await ConfigurationEditor(store).clear("web_max_calls_per_run", "ana")
    await config.refresh()

    assert config.current.values["web_max_calls_per_run"].value == 3
    assert config.current.values["web_max_calls_per_run"].source is SettingSource.DEFAULT


async def test_a_value_explains_where_it_came_from_and_who_changed_it() -> None:
    store = FakeConfigurationStore([stored("indexed_channel_ids", "7", by="ana")])
    config = configuration(store)
    await config.refresh()

    views = {view.key: view for view in config.current.explain()}

    assert views["indexed_channel_ids"].source is SettingSource.DATABASE
    assert views["indexed_channel_ids"].updated_by == "ana"
    assert views["indexed_channel_ids"].updated_at == AT
    assert views["ask_min_confidence"].source is SettingSource.DEFAULT
    assert views["ask_min_confidence"].updated_by is None
    # Every editable setting is explainable, not merely the stored ones.
    assert set(views) == set(SETTINGS)


# --- a change reaches a running process ---------------------------------


async def test_removing_a_channel_from_scope_narrows_without_a_restart() -> None:
    store = FakeConfigurationStore([stored("indexed_channel_ids", "100 200")])
    config = configuration(store, indexed_channel_ids="100 200")
    await config.refresh()

    # What a running job does: consult the snapshot each pass rather than
    # capture a value at construction.
    def is_indexed(channel: int) -> bool:
        return channel in config.current.get(INDEXED_CHANNEL_IDS)

    assert is_indexed(200)

    store.rows = [stored("indexed_channel_ids", "100")]
    report = await config.refresh()

    assert is_indexed(200) is False
    assert report.changed == ("indexed_channel_ids",)


async def test_revoking_a_tool_reaches_a_registered_observer() -> None:
    store = FakeConfigurationStore([stored("federation_tool_allowlist", "issues:list:ro")])
    config = configuration(store)
    await config.refresh()
    seen: list[tuple[str, ...]] = []
    config.on_change(lambda snapshot: seen.append(snapshot.get(FEDERATION_TOOL_ALLOWLIST)))

    store.rows = []
    await config.refresh()

    assert seen == [()]


async def test_an_observer_that_fails_neither_stops_the_others_nor_the_refresh() -> None:
    store = FakeConfigurationStore()
    config = configuration(store)
    await config.refresh()
    seen: list[str] = []

    def explode(_: object) -> None:
        raise RuntimeError("consumer is broken")

    config.on_change(explode)
    config.on_change(lambda _: seen.append("second"))

    store.rows = [stored("window_max_messages", "40")]
    report = await config.refresh()

    assert report.applied is True
    assert seen == ["second"]


async def test_the_refresh_loop_reads_before_its_first_sleep() -> None:
    store = FakeConfigurationStore([stored("window_max_tokens", "999")])
    config = configuration(store)

    task = asyncio.create_task(config.run_forever(interval=3600.0))
    for _ in range(20):
        await asyncio.sleep(0)
        if store.loads:
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert store.loads == 1
    assert config.current.values["window_max_tokens"].value == 999


# --- the failure rule ---------------------------------------------------


async def test_a_failed_refresh_keeps_the_current_configuration() -> None:
    store = FakeConfigurationStore([stored("indexed_channel_ids", "100")])
    config = configuration(store, indexed_channel_ids="100 200")
    await config.refresh()

    store.fail_with = ConnectionError("database is gone")
    report = await config.refresh()

    assert report.applied is False
    assert report.changed == ()
    assert config.current.get(INDEXED_CHANNEL_IDS) == {100}


async def test_a_failed_refresh_does_not_undo_a_narrowing_by_reverting_to_the_environment() -> None:
    """The whole reason the rule exists, stated as a test.

    An operator has just removed a channel from scope and revoked a tool. A
    database blip must not put either back: the dangerous direction is
    widening, so the safe default is to change nothing.
    """
    store = FakeConfigurationStore(
        [stored("indexed_channel_ids", "100"), stored("federation_tool_allowlist", "")]
    )
    config = configuration(
        store,
        environ={
            **LIVE_ENVIRON,
            "INDEXED_CHANNEL_IDS": "100 200",
            "FEDERATION_TOOL_ALLOWLIST": "issues:close:enable-mutation",
        },
        indexed_channel_ids="100 200",
        federation_tool_allowlist="issues:close:enable-mutation",
    )
    await config.refresh()

    store.fail_with = TimeoutError("connection pool exhausted")
    await config.refresh()

    assert 200 not in config.current.get(INDEXED_CHANNEL_IDS)
    assert config.current.get(FEDERATION_TOOL_ALLOWLIST) == ()


async def test_a_failed_refresh_never_raises_at_the_caller() -> None:
    """The loop that calls this has to outlive every failure it can meet."""
    store = FakeConfigurationStore()
    store.fail_with = RuntimeError("anything at all")
    config = configuration(store)

    assert (await config.refresh()).applied is False


# --- invalid stored configuration ---------------------------------------


async def test_a_malformed_value_keeps_the_previous_one_and_is_reported() -> None:
    store = FakeConfigurationStore([stored("window_max_messages", "40")])
    config = configuration(store)
    await config.refresh()

    store.rows = [stored("window_max_messages", "not a number")]
    report = await config.refresh()

    assert config.current.values["window_max_messages"].value == 40
    assert [(p.key, p.kind) for p in report.problems] == [
        ("window_max_messages", "value_rejected")
    ]


async def test_a_malformed_value_at_startup_leaves_the_environment_in_force() -> None:
    store = FakeConfigurationStore([stored("ask_min_confidence", "7.5")])
    config = configuration(
        store,
        environ={**LIVE_ENVIRON, "ASK_MIN_CONFIDENCE": "0.8"},
        ask_min_confidence=0.8,
    )

    report = await config.refresh()

    assert config.current.get(ASK_MIN_CONFIDENCE) == 0.8
    assert config.current.source(ASK_MIN_CONFIDENCE) is SettingSource.ENVIRONMENT
    assert report.applied is True
    assert report.problems[0].key == "ask_min_confidence"


async def test_one_bad_row_does_not_stop_the_good_ones_being_applied() -> None:
    store = FakeConfigurationStore(
        [stored("window_max_messages", "zero"), stored("indexed_channel_ids", "5")]
    )
    config = configuration(store)

    await config.refresh()

    assert config.current.get(INDEXED_CHANNEL_IDS) == {5}


async def test_a_row_for_a_setting_that_does_not_exist_is_reported_and_skipped() -> None:
    store = FakeConfigurationStore([stored("retention_forever", "true")])
    config = configuration(store)

    report = await config.refresh()

    assert report.applied is True
    assert [(p.key, p.kind) for p in report.problems] == [("retention_forever", "unknown_setting")]


# --- secrets ------------------------------------------------------------


@pytest.mark.parametrize("key", ["discord_token", "llm_api_key", "database_url"])
async def test_a_credential_stored_in_the_database_is_ignored_and_reported(key: str) -> None:
    """Reachable only by writing to the table directly. Checked anyway.

    A resolver that obeyed such a row would let anyone with a database
    connection swap the bot's identity or point the model calls elsewhere.
    """
    store = FakeConfigurationStore([stored(key, "stolen-value")])
    config = configuration(store)

    report = await config.refresh()

    assert key not in config.current.values
    assert [(p.key, p.kind) for p in report.problems] == [(key, "secret_ignored")]


@pytest.mark.parametrize("key", ["discord_token", "llm_api_key", "database_url", "serpapi_key"])
async def test_storing_a_credential_is_refused_and_recorded(key: str) -> None:
    store = FakeConfigurationStore()

    with pytest.raises(SecretSetting) as refusal:
        await ConfigurationEditor(store).set(key, "a-real-looking-secret", "ana")

    assert key in str(refusal.value)
    assert store.puts == []
    assert [(k, operator) for k, operator, _ in store.refusals] == [(key, "ana")]


@pytest.mark.parametrize("key", ["discord_token", "llm_api_key", "database_url", "serpapi_key"])
async def test_a_refusal_never_records_the_value_that_was_refused(key: str) -> None:
    store = FakeConfigurationStore()
    secret = "sk-do-not-write-this-down"

    with pytest.raises(SecretSetting):
        await ConfigurationEditor(store).set(key, secret, "ana")

    assert all(secret not in field for entry in store.refusals for field in entry)


def test_no_credential_is_also_an_editable_setting() -> None:
    """Two registries, one of which must never grow into the other."""
    assert set(SECRETS) & set(SETTINGS) == set()


@pytest.mark.parametrize(
    ("key", "env_var"),
    [
        ("discord_token", "DISCORD_TOKEN"),
        ("llm_api_key", "LLM_API_KEY"),
        ("database_url", "DATABASE_URL"),
    ],
)
def test_startup_fails_naming_a_required_credential_that_is_absent(
    key: str, env_var: str
) -> None:
    environ = {k: v for k, v in LIVE_ENVIRON.items() if k != env_var}

    with pytest.raises(MissingSecret) as failure:
        check_required_secrets(environ)

    assert key in str(failure.value)
    assert env_var in str(failure.value)


def test_a_blank_credential_counts_as_absent() -> None:
    """A platform that renders every variable writes a blank for the unset ones."""
    with pytest.raises(MissingSecret) as failure:
        check_required_secrets({**LIVE_ENVIRON, "LLM_API_KEY": "   "})

    assert "llm_api_key" in str(failure.value)


def test_an_optional_credential_does_not_stop_startup() -> None:
    check_required_secrets(LIVE_ENVIRON)


# --- editing ------------------------------------------------------------


async def test_a_stored_edit_is_readable_by_the_next_refresh() -> None:
    store = FakeConfigurationStore()
    config = configuration(store)

    await ConfigurationEditor(store).set("indexed_channel_ids", "11, 12", "ana")
    await config.refresh()

    assert config.current.get(INDEXED_CHANNEL_IDS) == {11, 12}
    assert store.puts == [("indexed_channel_ids", "11, 12", "ana")]


async def test_a_malformed_edit_is_refused_while_the_operator_is_watching() -> None:
    store = FakeConfigurationStore()

    with pytest.raises(MalformedSetting):
        await ConfigurationEditor(store).set("ask_min_confidence", "2.0", "ana")

    assert store.puts == []
    assert store.refusals[0][0] == "ask_min_confidence"


async def test_a_setting_that_does_not_exist_is_refused() -> None:
    store = FakeConfigurationStore()

    with pytest.raises(UnknownSetting):
        await ConfigurationEditor(store).set("retention_forever", "true", "ana")

    assert store.puts == []
    assert store.refusals[0][0] == "retention_forever"


@pytest.mark.parametrize(
    "key",
    [
        "ask_min_confidence",
        "web_timeout_seconds",
        "window_max_messages",
        "ask_extraction_enabled",
        "federation_max_tools_per_run",
    ],
)
async def test_a_malformed_value_is_never_quoted_back_or_recorded(key: str) -> None:
    """A credential pasted into the wrong box must not become a stored secret.

    The refused text is not a secret *setting* -- the key is an ordinary one --
    so nothing on the secret path protects it. What reaches the record is the
    parse failure, and `int()`/`float()` put the text they were handed straight
    into their message. `config_audit` is append-only, so a value that lands
    there cannot be scrubbed afterwards.
    """
    store = FakeConfigurationStore()
    secret = "sk-live-9f3a-REAL-MODEL-KEY-zzz"

    with pytest.raises(MalformedSetting) as refusal:
        await ConfigurationEditor(store).set(key, secret, "ana")

    assert secret not in str(refusal.value)
    assert store.puts == []
    assert store.refusals, "the refusal itself must still be recorded"
    assert all(secret not in field for entry in store.refusals for field in entry)


async def test_a_refused_value_still_says_what_would_have_been_accepted() -> None:
    """Not quoting the value must not degrade into an unactionable refusal."""
    store = FakeConfigurationStore()

    with pytest.raises(MalformedSetting) as refusal:
        await ConfigurationEditor(store).set("ask_min_confidence", "2.0", "ana")

    assert "between 0 and 1" in str(refusal.value)


async def test_a_malformed_stored_row_is_reported_without_its_value() -> None:
    """The row was written directly, so its text is equally untrusted.

    `RefreshReport.problems` is logged and served to operators, which is the
    same disclosure by a different door.
    """
    secret = "sk-live-9f3a-REAL-MODEL-KEY-zzz"
    store = FakeConfigurationStore([stored("window_max_messages", secret)])
    config = configuration(store)

    report = await config.refresh()

    assert [(p.key, p.kind) for p in report.problems] == [("window_max_messages", "value_rejected")]
    assert secret not in report.problems[0].detail


async def test_clearing_something_that_is_not_a_setting_is_refused_too() -> None:
    store = FakeConfigurationStore()

    with pytest.raises(SecretSetting):
        await ConfigurationEditor(store).clear("discord_token", "ana")

    assert store.cleared == []


# --- parity with the environment ----------------------------------------
#
# A stored value and an environment variable have to mean the same thing. They
# are parsed by different code, so the parity is asserted rather than assumed:
# a divergence would show up as a setting that behaves differently depending on
# where it was configured, which is the hardest kind of bug to be told about.


@pytest.mark.parametrize(
    ("key", "raw"),
    [
        ("indexed_channel_ids", "100 200"),
        ("indexed_channel_ids", "100,200"),
        ("indexed_channel_ids", "100, 200  300"),
        ("federation_servers", "issues=https://issues.internal/mcp"),
        ("federation_tool_allowlist", "issues:list:ro, issues:close:enable-mutation"),
        ("federation_credential_holders", "issues=discord:1001"),
        ("ask_extraction_enabled", "false"),
        ("web_tools_enabled", "true"),
        ("ask_min_confidence", "0.75"),
        ("window_gap_seconds", "600"),
        ("web_timeout_seconds", "2.5"),
    ],
)
def test_a_stored_value_parses_exactly_as_the_environment_variable_does(
    key: str, raw: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name, value in LIVE_ENVIRON.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv(key.upper(), raw)
    from_environment = getattr(Settings(_env_file=None), key)  # type: ignore[call-arg]

    assert SETTINGS[key].parse(raw) == from_environment


def test_every_editable_setting_names_a_field_that_exists() -> None:
    """A spec keyed on a field nobody has would resolve to an AttributeError.

    It would do so on the first refresh of a running process, which is the
    worst place to discover a typo.
    """
    assert set(SETTINGS) <= set(Settings.model_fields)


def test_the_environment_baseline_covers_every_editable_setting() -> None:
    baseline = environment_snapshot(settings(), LIVE_ENVIRON)

    assert set(baseline) == set(SETTINGS)
