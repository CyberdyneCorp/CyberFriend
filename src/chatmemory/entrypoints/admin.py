"""The admin console service: the API, the interface, and nothing else.

This process holds **database credentials**, and -- when CyberdyneAuth sign-in
is configured -- the OIDC client secret and the key its sessions are encrypted
under. It has no Discord token, no model key and no platform access, and that
is the point of the whole change: a console that could redeploy would need
platform credentials, which is a far larger authority than configuring the
agent requires -- and is exactly why stored configuration exists instead of
the console writing environment variables. Neither sign-in secret reaches the
corpus or the bot's accounts: one lets this process act as the `cyberfriend`
client at CyberdyneAuth, the other decrypts the tokens in `admin_session`.
Both are in `admin.oidc.config.SECRET_VARS` and are never logged or shown.

Its only outbound HTTP is to CyberdyneAuth (discovery, keys, token, userinfo,
revocation), through the transport `build` is given, like the bot's
`Edges.http_transport`.

Two consequences shape this file:

*   **It never loads `Settings`.** That class requires the Discord token, the
    guild id and the model key, so importing the application settings here
    would couple starting the console to holding the credentials it is built
    not to hold. The baseline it needs -- what the *environment* says each
    editable setting is -- is read from `os.environ` through the same parsers
    the resolver uses, with the field defaults taken from `Settings` without
    instantiating it. Migrations make the same argument for the same reason.

*   **It re-reads stored configuration on a cadence, like every other
    service.** The console shows what is in force, not what was in force when
    it started, and an operator who edits a setting and sees the old value
    concludes the edit did not save.

The compose file gives this service the editable settings as well as the
database URL. Without them its environment baseline would be all defaults, and
every setting an operator has configured in the platform would be reported as
"default" on the one screen built to explain where values come from.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import httpx
import structlog
import uvicorn
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from starlette.applications import Starlette

from chatmemory import logging as log_setup
from chatmemory.adapters.documents.store import PostgresDocumentStore
from chatmemory.adapters.store.admin_postgres import (
    PostgresChangeRecord,
    PostgresOperatorTokens,
)
from chatmemory.adapters.store.admin_session_postgres import (
    PostgresLoginStore,
    PostgresSessionStore,
)
from chatmemory.adapters.store.config_postgres import PostgresConfigurationStore
from chatmemory.adapters.store.retention_sql import PostgresRetentionStore
from chatmemory.adapters.store.trace_postgres import PostgresTraceIndex
from chatmemory.adapters.tracing.langfuse import warn_unless_supported
from chatmemory.admin.handlers.federation import make_probe
from chatmemory.admin.handlers.queries import (
    PostgresChannelDirectory,
    PostgresCorpusStatus,
    PostgresOptOutDirectory,
)
from chatmemory.admin.handlers.services import AdminServices
from chatmemory.admin.oidc.config import (
    ISSUER_VAR,
    SignInSettings,
    sign_in_settings,
)
from chatmemory.admin.oidc.provider import OIDCProvider, ProviderUnavailable
from chatmemory.admin.oidc.service import SignIn
from chatmemory.admin.server import build_app
from chatmemory.app.configuration import (
    SETTINGS,
    ConfigurationEditor,
    RuntimeConfiguration,
)
from chatmemory.app.optout import OptOutService
from chatmemory.config import Settings
from chatmemory.mcp.auth import PostgresTokenStore
from chatmemory.ports.configuration import ResolvedValue, SettingSource

log = structlog.get_logger()

DEFAULT_PORT = 8083
"""Its own port, and in the compose file its own domain.

Read from the environment rather than from `Settings`, like the MCP service's
host allowlist: how a service is published is a property of the deployment,
not of the corpus.
"""

CONSOLE_DIR_VAR = "ADMIN_CONSOLE_DIR"
PORT_VAR = "ADMIN_PORT"
DATABASE_URL_VAR = "DATABASE_URL"
OIDC_ISSUER_VAR = ISSUER_VAR
"""The CyberdyneAuth issuer. Unset: `cfa_` tokens are admin, as before roles.

Set (with the client id, secret, session key and public URL, see
`admin.oidc.config`): people sign in with CyberdyneAuth, tokens are operator
(read-only), and admin rights come only from the identity provider's role.
Unsetting it again is the break-glass rollback; the other four may stay set.
"""

FORBIDDEN_CREDENTIALS = ("DISCORD_TOKEN", "LLM_API_KEY", "SERPAPI_KEY")
"""Credentials this service must not be given.

Checked at startup and reported, not enforced -- the deployment decides what
is in the environment, and a console that refused to start because somebody
exported a variable would be an outage caused by a warning. What it must not
do is hold one silently: a console with the bot token can impersonate the bot
everywhere it is installed.
"""


class MissingDatabase(RuntimeError):
    """The one credential this service does need is absent."""


def environment_baseline(environ: Mapping[str, str]) -> dict[str, ResolvedValue]:
    """What the environment says each editable setting is, without `Settings`.

    `app.configuration.environment_snapshot` does this from a loaded
    `Settings`, which cannot be built without the Discord and model
    credentials. This service holds neither, so it reads the same variables
    through the same per-setting parsers and takes the defaults from the field
    definitions -- `model_fields` is class data, so nothing is validated and no
    credential is required.

    A value the environment sets but cannot be parsed falls back to the
    default and is reported. The alternative is a console that will not start
    because of one bad variable, which is the least useful moment for it to be
    unavailable.
    """
    resolved: dict[str, ResolvedValue] = {}
    for key, spec in SETTINGS.items():
        default = Settings.model_fields[key].get_default(call_default_factory=True)
        raw = environ.get(spec.env_var, "").strip()
        if not raw:
            resolved[key] = ResolvedValue(default, SettingSource.DEFAULT)
            continue
        try:
            resolved[key] = ResolvedValue(spec.parse(raw), SettingSource.ENVIRONMENT)
        except (ValueError, TypeError):
            # The spec's wording rather than the parse failure's: a variable
            # holding something that was never meant to be one -- a key pasted
            # into the wrong line of a deployment -- must not be copied into
            # the log by the line that reports it.
            log.warning(
                "admin.environment_value_rejected", setting=key, detail=f"expected {spec.expects}"
            )
            resolved[key] = ResolvedValue(default, SettingSource.DEFAULT)
    return resolved


def console_dir(environ: Mapping[str, str]) -> Path | None:
    raw = environ.get(CONSOLE_DIR_VAR, "").strip()
    return Path(raw) if raw else None


def port(environ: Mapping[str, str]) -> int:
    raw = environ.get(PORT_VAR, "").strip()
    return int(raw) if raw.isdigit() else DEFAULT_PORT


def oidc_issuer(environ: Mapping[str, str]) -> str | None:
    return environ.get(OIDC_ISSUER_VAR, "").strip() or None


def sign_in(
    settings: SignInSettings | None,
    engine: AsyncEngine,
    transport: httpx.AsyncBaseTransport | None,
) -> SignIn | None:
    """CyberdyneAuth sign-in over Postgres, or None when it is not configured."""
    if settings is None:
        return None
    provider = OIDCProvider(settings, transport=transport)
    return SignIn(settings, provider, PostgresLoginStore(engine), PostgresSessionStore(engine))


def database_url(environ: Mapping[str, str]) -> str:
    url = environ.get(DATABASE_URL_VAR, "").strip()
    if not url:
        raise MissingDatabase(
            f"{DATABASE_URL_VAR} is required; the console holds database credentials "
            "and no others"
        )
    return url


def warn_about_credentials(environ: Mapping[str, str]) -> None:
    held = [name for name in FORBIDDEN_CREDENTIALS if environ.get(name, "").strip()]
    if held:
        log.error(
            "admin.holds_credentials_it_should_not",
            credentials=held,
            hint="the console needs only DATABASE_URL; remove these from its environment",
        )


@dataclass(frozen=True, slots=True)
class ConsoleProcess:
    """The assembled service, before anything starts serving.

    Returned as one value so the wiring can be built and inspected without
    binding a port -- which is what lets a test assert that the running
    process reaches the Postgres adapters, rather than asserting that a
    function exists which would have.
    """

    app: Starlette
    #: The ports the running process holds. Carried so a test can assert that
    #: the service reaches the Postgres adapters rather than asserting that a
    #: function exists which would have built them.
    services: AdminServices
    configuration: RuntimeConfiguration
    engine: AsyncEngine
    port: int
    #: CyberdyneAuth sign-in, or None when it is not configured.
    sign_in: SignIn | None = None


def build(
    environ: Mapping[str, str], *, transport: httpx.AsyncBaseTransport | None = None
) -> ConsoleProcess:
    """Wire the console from the environment. Connects to nothing yet.

    `create_async_engine` is lazy, so this does no I/O: the first connection
    happens on the first request or the first refresh, and CyberdyneAuth's
    discovery on the first sign-in (or `main`'s warm-up). `transport` carries
    every outbound call to CyberdyneAuth; None is the real network.

    An issuer without the other sign-in settings raises `MisconfiguredSignIn`,
    naming what is missing: the issuer alone would downscope every token to
    operator with no way left to sign in as admin. No issuer is sign-in off,
    whatever else is set -- the break-glass rollback.
    """
    warn_about_credentials(environ)
    settings = sign_in_settings(environ)

    # No schema creation here: `app_setting`, `admin_token` and `config_audit`
    # belong to migrations 0011 and 0012. A service that creates its own tables
    # hides an unmigrated database until the first query against a table it did
    # not think to create.
    engine = create_async_engine(database_url(environ), pool_pre_ping=True)
    store = PostgresConfigurationStore(engine)
    configuration = RuntimeConfiguration(store, environment_baseline(environ))
    retention = PostgresRetentionStore(engine)

    services = AdminServices(
        configuration=configuration,
        editor=ConfigurationEditor(store),
        changes=PostgresChangeRecord(engine),
        status=PostgresCorpusStatus(engine),
        channels=PostgresChannelDirectory(engine),
        optout_directory=PostgresOptOutDirectory(engine),
        # With the document store, not without it: an opt-out that covers
        # messages and leaves the PDF somebody attached fully searchable has
        # withdrawn the index entry and kept the content. With the trace
        # index too: the opt-out marks the person's exported runs, and ingest,
        # which holds the Langfuse keys, deletes them.
        optouts=OptOutService(
            retention, PostgresDocumentStore(engine), PostgresTraceIndex(engine)
        ),
        mcp_tokens=PostgresTokenStore(engine),
        probe=make_probe(),
    )
    signing_in = sign_in(settings, engine, transport)
    return ConsoleProcess(
        app=build_app(
            services,
            PostgresOperatorTokens(engine),
            console_dir(environ),
            oidc_configured=settings is not None,
            sign_in=signing_in,
        ),
        services=services,
        configuration=configuration,
        engine=engine,
        port=port(environ),
        sign_in=signing_in,
    )


async def warm_up(signing_in: SignIn | None) -> None:
    """Fetch discovery at startup, so a bad issuer shows in the log at once.

    Best effort: CyberdyneAuth being down must not stop the console, whose
    bearer tokens keep working without it. Sessions do not: discovery and the
    key set are cached in memory only, so a process started during an outage
    refuses every session request until CyberdyneAuth answers.
    """
    if signing_in is None:
        return
    try:
        await signing_in.provider.discovery()
    except ProviderUnavailable as exc:
        log.error("admin.oidc.discovery_failed", error=str(exc))


async def main() -> None:
    log_setup.configure()
    built = build(os.environ)
    log.info(
        "admin.starting",
        port=built.port,
        console=str(console_dir(os.environ) or "not configured"),
        settings=len(SETTINGS),
        token_role="operator" if built.sign_in else "admin",
        sign_in="cyberdyneauth" if built.sign_in else "off",
    )
    await warm_up(built.sign_in)
    if langfuse_host := os.environ.get("LANGFUSE_HOST"):
        await warn_unless_supported(langfuse_host)
    config = uvicorn.Config(
        built.app, host="0.0.0.0", port=built.port, log_level="warning"
    )
    await asyncio.gather(
        # The same bounded refresh every long-running process runs. Without it
        # the console would report the configuration it started with, which is
        # the one thing an operator must never be shown here.
        built.configuration.run_forever(),
        uvicorn.Server(config).serve(),
    )


if __name__ == "__main__":
    asyncio.run(main())
