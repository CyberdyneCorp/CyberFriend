"""Is the console actually wired, and is it wired to nothing it must not hold.

This project's recurring defect is a feature that ships implemented, tested and
connected to nothing. So these tests are about the *process*: the entrypoint
builds the real Postgres adapters, the application it hands to uvicorn carries
every route in the contract, and the refresh loop that keeps configuration
current is started by the same function that starts the server.

The other half is what the process must not be able to reach. It holds database
credentials and no others, so the Discord SDK is never imported into it and the
application settings -- which require the bot token and the model key -- are
never loaded.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest
from starlette.routing import Route
from starlette.testclient import TestClient

from chatmemory.adapters.store.admin_postgres import (
    PostgresChangeRecord,
    PostgresOperatorTokens,
)
from chatmemory.adapters.store.admin_session_postgres import (
    PostgresLoginStore,
    PostgresSessionStore,
)
from chatmemory.adapters.store.config_postgres import PostgresConfigurationStore
from chatmemory.admin.auth import Operator
from chatmemory.admin.handlers.queries import (
    PostgresChannelDirectory,
    PostgresCorpusStatus,
    PostgresOptOutDirectory,
)
from chatmemory.admin.oidc.config import MisconfiguredSignIn
from chatmemory.entrypoints import admin
from chatmemory.mcp.auth import PostgresTokenStore
from chatmemory.ports.configuration import SettingSource
from tests.e2e.harness.oidc import FakeOIDC
from tests.unit.oidc_support import ENVIRON as SIGN_IN
from tests.unit.oidc_support import PUBLIC_URL

ENTRYPOINT = Path(admin.__file__)

ENVIRON = {
    "DATABASE_URL": "postgresql+asyncpg://console:console@db/chatmemory",
    "INDEXED_CHANNEL_IDS": "100 200",
    "ASK_MIN_CONFIDENCE": "0.75",
    "ADMIN_PORT": "8083",
}

#: The contract, as the operator interface and the API were both written to it.
#: A route that is missing here is a screen with no server behind it; a route
#: here that the app does not serve is the same defect in the other direction.
CONTRACT = [
    ("GET", "/api/status"),
    ("GET", "/api/settings"),
    ("PUT", "/api/settings/{key}"),
    ("GET", "/api/federation/servers"),
    ("POST", "/api/federation/servers"),
    ("DELETE", "/api/federation/servers/{name}"),
    ("GET", "/api/federation/allowlist"),
    ("POST", "/api/federation/allowlist"),
    ("DELETE", "/api/federation/allowlist/{server}/{tool}"),
    ("GET", "/api/channels"),
    ("POST", "/api/channels"),
    ("DELETE", "/api/channels/{id}"),
    ("GET", "/api/optouts"),
    ("POST", "/api/optouts"),
    ("DELETE", "/api/optouts/{platform}/{id}"),
    ("GET", "/api/tokens"),
    # No POST: minting an MCP credential grants a read of one Discord
    # account's whole view of the corpus, and the console has no route to the
    # corpus. `test_admin_api.py` asserts the refusal; this list is the
    # contract the interface is written against, so the absence belongs here
    # too -- a screen that offered to issue one would have no server behind it.
    ("DELETE", "/api/tokens/{id}"),
    ("GET", "/api/audit"),
]


def built() -> admin.ConsoleProcess:
    """The service as the container starts it. Connects to nothing."""
    return admin.build(ENVIRON)


# --- the process reaches the real adapters -----------------------------


def test_the_service_is_built_from_the_postgres_adapters() -> None:
    process = built()

    assert isinstance(process.services.status, PostgresCorpusStatus)
    assert isinstance(process.services.channels, PostgresChannelDirectory)
    assert isinstance(process.services.optout_directory, PostgresOptOutDirectory)
    assert isinstance(process.services.changes, PostgresChangeRecord)
    assert isinstance(process.services.mcp_tokens, PostgresTokenStore)
    assert isinstance(process.services.editor.store, PostgresConfigurationStore)


def test_the_opt_out_service_covers_documents() -> None:
    """Messages only is not an opt-out.

    An opt-out that leaves the PDF somebody attached fully searchable has
    withdrawn the index entry and kept the content.
    """
    process = built()

    assert process.services.optouts._documents is not None  # noqa: SLF001


def test_the_console_authenticates_with_the_postgres_operator_tokens() -> None:
    source = ENTRYPOINT.read_text()

    assert "PostgresOperatorTokens(engine)" in source
    assert PostgresOperatorTokens.__name__ in source


@pytest.mark.parametrize(("method", "path"), CONTRACT)
def test_every_route_in_the_contract_is_served(method: str, path: str) -> None:
    served = {
        (verb, route.path)
        for route in built().app.routes
        if isinstance(route, Route)
        for verb in (route.methods or set())
    }

    assert (method, path) in served


def test_the_refresh_loop_runs_beside_the_server() -> None:
    """Without it the console reports the configuration it started with.

    Asserted on the source because starting `main` binds a port; what matters
    is that the same gather starts both, which is exactly what this reads.
    """
    tree = ast.parse(ENTRYPOINT.read_text())
    main = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "main"
    )
    gathered = ast.dump(main)

    assert "run_forever" in gathered
    assert "serve" in gathered


# --- credentials the console must not hold -----------------------------


def test_the_entrypoint_never_loads_the_application_settings() -> None:
    """`Settings` requires the Discord token and the model key.

    Loading it here would couple starting the console to holding the very
    credentials it exists not to hold -- the same coupling that once made a
    missing bot token fail a schema migration.
    """
    source = ENTRYPOINT.read_text()

    assert "get_settings" not in source
    # The class is referenced for its field *defaults*, which is class data and
    # validates nothing.
    assert "Settings.model_fields" in source


def test_starting_the_console_needs_no_credential_but_the_database() -> None:
    process = admin.build({"DATABASE_URL": ENVIRON["DATABASE_URL"]})

    assert process.port == admin.DEFAULT_PORT


def test_a_console_without_a_database_url_says_so() -> None:
    with pytest.raises(admin.MissingDatabase) as refusal:
        admin.build({})

    assert "DATABASE_URL" in str(refusal.value)


def test_the_discord_sdk_is_never_imported_into_this_process() -> None:
    """A console that can reach Discord can impersonate the bot.

    Run in a subprocess because another test in this session may already have
    imported the SDK for its own reasons; what matters is what *this*
    entrypoint pulls in on its own.
    """
    probe = (
        "import sys; import chatmemory.entrypoints.admin as a; "
        "print('discord' in sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )

    assert result.stdout.strip() == "False", result.stdout


def test_holding_a_credential_it_should_not_is_reported() -> None:
    """Reported, not enforced: the deployment decides what is in the
    environment, and refusing to start would be an outage caused by a warning.
    """
    assert admin.FORBIDDEN_CREDENTIALS == ("DISCORD_TOKEN", "LLM_API_KEY", "SERPAPI_KEY")
    # Builds anyway, with the error logged.
    assert admin.build({**ENVIRON, "DISCORD_TOKEN": "zzz"}).port == 8083


# --- the environment baseline ------------------------------------------


def test_the_baseline_reports_what_the_environment_set() -> None:
    baseline = admin.environment_baseline(ENVIRON)

    assert baseline["indexed_channel_ids"].value == frozenset({100, 200})
    assert baseline["indexed_channel_ids"].source is SettingSource.ENVIRONMENT
    assert baseline["ask_min_confidence"].value == 0.75


def test_the_baseline_reports_a_default_as_a_default() -> None:
    """The distinction the settings screen exists to draw.

    A value nobody chose and a value somebody chose that happens to match are
    different answers to "why is it this".
    """
    baseline = admin.environment_baseline(ENVIRON)

    assert baseline["window_max_messages"].source is SettingSource.DEFAULT


def test_the_baseline_covers_every_editable_setting() -> None:
    from chatmemory.app.configuration import SETTINGS

    assert set(admin.environment_baseline({})) == set(SETTINGS)


def test_the_baseline_carries_no_credential() -> None:
    from chatmemory.app.configuration import SECRETS

    baseline = admin.environment_baseline(
        {**ENVIRON, "DISCORD_TOKEN": "zzz", "LLM_API_KEY": "sk-zzz"}
    )

    assert not set(baseline) & set(SECRETS)
    assert "zzz" not in repr(baseline)


def test_one_unreadable_variable_does_not_stop_the_console_starting() -> None:
    """The least useful moment for the console to be unavailable is the one
    where somebody has just mistyped a variable."""
    baseline = admin.environment_baseline({**ENVIRON, "ASK_MIN_CONFIDENCE": "banana"})

    assert baseline["ask_min_confidence"].source is SettingSource.DEFAULT
    assert baseline["indexed_channel_ids"].value == frozenset({100, 200})


def test_the_baseline_agrees_with_the_settings_class() -> None:
    """The console must describe the same world the bot runs in.

    Parsed here and validated there; if the two disagree, the screen that
    explains where a value came from explains the wrong value.
    """
    from chatmemory.config import Settings

    settings = Settings(  # type: ignore[call-arg]
        discord_token="x",
        discord_guild_id=1,
        database_url="postgresql+asyncpg://u:p@h/d",
        llm_api_key="k",
        indexed_channel_ids="100 200",
        ask_min_confidence="0.75",
    )
    baseline = admin.environment_baseline(ENVIRON)

    assert baseline["indexed_channel_ids"].value == settings.indexed_channel_ids
    assert baseline["ask_min_confidence"].value == settings.ask_min_confidence


# --- the interface -----------------------------------------------------


def test_the_console_bundle_is_served_when_it_is_there(tmp_path: Path) -> None:
    """One container: the API serves the interface it belongs to.

    A separate static host would need its own domain, its own certificate and
    a CORS policy wide enough for a browser to cross between them -- on the
    highest-privilege surface in the system.
    """
    (tmp_path / "index.html").write_text("<!doctype html><title>console</title>")
    process = admin.build({**ENVIRON, "ADMIN_CONSOLE_DIR": str(tmp_path)})

    response = TestClient(process.app).get("/")

    assert response.status_code == 200
    assert "console" in response.text


def test_a_missing_bundle_says_so_rather_than_404() -> None:
    """The deploy where the bundle did not get copied in must read as that.

    Every path returning "not found" reads as a broken API instead.
    """
    response = TestClient(built().app).get("/")

    assert response.status_code == 200
    assert "interface bundle is not here" in response.text


# --- ADMIN_OIDC_ISSUER reaches the token role -----------------------------


@pytest.fixture
def any_token_is_ana(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every `cfa_` token resolves to one operator, without a database."""

    async def operator_for_token(_self: object, _token: str) -> Operator:
        return Operator("ana")

    monkeypatch.setattr(PostgresOperatorTokens, "operator_for_token", operator_for_token)


def _opt_out_as_a_token(environ: dict[str, str]) -> tuple[int, object]:
    # An empty body: past the role check the handler refuses it with 400
    # before touching the database, so the status alone says which side of
    # the role check the request landed on.
    client = TestClient(admin.build(environ).app, raise_server_exceptions=False)
    response = client.post(
        "/api/optouts", json={}, headers={"Authorization": "Bearer cfa_anything"}
    )
    return response.status_code, response.json()


@pytest.mark.usefixtures("any_token_is_ana")
def test_configuring_sign_in_makes_every_token_operator_only() -> None:
    """The production path: the variables, through build(), to the middleware."""
    status, body = _opt_out_as_a_token({**ENVIRON, **SIGN_IN})

    assert (status, body) == (403, {"error": "requires admin"})


@pytest.mark.parametrize("missing", sorted(set(SIGN_IN) - {"ADMIN_OIDC_ISSUER"}))
def test_sign_in_partly_configured_refuses_to_start(missing: str) -> None:
    """The issuer without the rest would leave nobody able to sign in as admin."""
    environ = {**ENVIRON, **{k: v for k, v in SIGN_IN.items() if k != missing}}

    with pytest.raises(MisconfiguredSignIn, match=missing):
        admin.build(environ)


@pytest.mark.usefixtures("any_token_is_ana")
def test_unsetting_only_the_issuer_is_the_break_glass_rollback() -> None:
    """The documented rollback: the other four stay set, and the console starts
    with sign-in off and `cfa_` tokens admin again, rather than refusing to
    start at the moment an urgent change is needed."""
    environ = {**ENVIRON, **{k: v for k, v in SIGN_IN.items() if k != "ADMIN_OIDC_ISSUER"}}

    process = admin.build(environ)
    status, _ = _opt_out_as_a_token(environ)

    assert process.sign_in is None
    assert status == 400  # past the admin check
    login = TestClient(process.app, base_url=PUBLIC_URL).get(
        "/auth/login", follow_redirects=False
    )
    assert login.status_code == 404


def test_sign_in_is_wired_to_postgres_and_the_injected_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_database(_self: object, *_args: object) -> None:
        return None

    monkeypatch.setattr(PostgresLoginStore, "begin", no_database)
    fake = FakeOIDC()
    process = admin.build({**ENVIRON, **SIGN_IN}, transport=fake.transport)

    assert process.sign_in is not None
    assert isinstance(process.sign_in._sessions, PostgresSessionStore)  # noqa: SLF001
    assert isinstance(process.sign_in._logins, PostgresLoginStore)  # noqa: SLF001
    response = TestClient(process.app, base_url=PUBLIC_URL).get(
        "/auth/login", follow_redirects=False
    )
    # Discovery went through the injected transport: the redirect names the
    # fake issuer's authorization endpoint.
    assert response.status_code == 302
    assert response.headers["location"].startswith("https://auth.test/authorize?")


def test_sign_in_secrets_are_never_recorded() -> None:
    from chatmemory.admin.audit import SECRET_SETTINGS

    assert {"admin_oidc_client_secret", "admin_session_key"} <= SECRET_SETTINGS


@pytest.mark.usefixtures("any_token_is_ana")
@pytest.mark.parametrize("issuer", [None, "", "   "])
def test_without_an_issuer_a_token_stays_admin(issuer: str | None) -> None:
    """Unset or blank is not an issuer: downscoping then would lock everyone out."""
    environ = dict(ENVIRON) if issuer is None else {**ENVIRON, "ADMIN_OIDC_ISSUER": issuer}

    status, _ = _opt_out_as_a_token(environ)

    assert status == 400
