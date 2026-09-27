"""The CyberdyneAuth provisioning adapter against a MockTransport of the approved contract.

The fake issuer below is the contract as approved: discovery naming the token
endpoint, a `client_credentials` grant for the provisioning client scoped
`users:provision`, and `POST /api/v1/users/provision` answering 202 whatever it
did. Each test changes one answer and checks what the adapter makes of it.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest
import structlog
from pydantic import ValidationError

from chatmemory.adapters.accounts.cyberdyneauth import (
    CyberdyneAuthProvisioner,
    ProvisioningClient,
    provision_body,
)
from chatmemory.composition import build_account_provisioner
from chatmemory.config import Settings
from chatmemory.ports.accounts import (
    ProvisioningInvalidName,
    ProvisioningRateLimited,
    ProvisioningRequest,
    ProvisioningUnavailable,
)

ISSUER = "https://auth.example.com"
TOKEN_URL = f"{ISSUER}/oauth/token"
PROVISION_URL = f"{ISSUER}/api/v1/users/provision"
CLIENT = ProvisioningClient(issuer=ISSUER, client_id="cf-provision", client_secret="s3cret")
REQUEST = ProvisioningRequest(email="leo@example.com", name="Leo Araujo", locale="pt-BR")


@dataclass
class FakeCyberdyneAuth:
    """The approved contract, with the knobs a test turns."""

    provision_statuses: list[int] = field(default_factory=lambda: [202])
    token_status: int = 200
    expires_in: int | None = 300
    discovered_issuer: str = ISSUER
    auth_methods: list[str] = field(default_factory=lambda: ["client_secret_basic"])
    token_requests: list[httpx.Request] = field(default_factory=list)
    provisions: list[httpx.Request] = field(default_factory=list)
    issued: int = 0

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == f"{ISSUER}/.well-known/openid-configuration":
            return httpx.Response(200, json=self._discovery())
        if url == TOKEN_URL:
            return self._token(request)
        if url == PROVISION_URL and request.method == "POST":
            self.provisions.append(request)
            # The last status repeats for every later call.
            statuses = self.provision_statuses
            status = statuses.pop(0) if len(statuses) > 1 else statuses[0]
            return httpx.Response(status, json={"status": "accepted"} if status == 202 else {})
        return httpx.Response(404)

    def _discovery(self) -> dict[str, Any]:
        return {
            "issuer": self.discovered_issuer,
            "token_endpoint": TOKEN_URL,
            "token_endpoint_auth_methods_supported": self.auth_methods,
        }

    def _token(self, request: httpx.Request) -> httpx.Response:
        self.token_requests.append(request)
        if self.token_status != 200:
            return httpx.Response(self.token_status, json={"error": "invalid_client"})
        self.issued += 1
        body: dict[str, Any] = {"access_token": f"tok-{self.issued}", "token_type": "Bearer"}
        if self.expires_in is not None:
            body["expires_in"] = self.expires_in
        return httpx.Response(200, json=body)


class Monotonic:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def provisioner(
    fake: FakeCyberdyneAuth, clock: Monotonic | None = None
) -> CyberdyneAuthProvisioner:
    return CyberdyneAuthProvisioner(
        CLIENT, transport=fake.transport(), monotonic=clock or Monotonic()
    )


def form(request: httpx.Request) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(request.content.decode()).items()}


async def test_202_is_accepted_and_only_email_name_and_locale_are_sent() -> None:
    fake = FakeCyberdyneAuth()

    await provisioner(fake).request_account(REQUEST)

    (sent,) = fake.provisions
    assert json.loads(sent.content) == {
        "email": "leo@example.com",
        "name": "Leo Araujo",
        "locale": "pt-BR",
    }
    assert sent.headers["Authorization"] == "Bearer tok-1"


async def test_the_token_is_client_credentials_for_users_provision_only() -> None:
    fake = FakeCyberdyneAuth()

    await provisioner(fake).request_account(REQUEST)

    (token_request,) = fake.token_requests
    assert form(token_request) == {"grant_type": "client_credentials", "scope": "users:provision"}
    basic = base64.b64encode(b"cf-provision:s3cret").decode()
    assert token_request.headers["Authorization"] == f"Basic {basic}"


async def test_client_secret_post_when_the_issuer_offers_only_that() -> None:
    fake = FakeCyberdyneAuth(auth_methods=["client_secret_post"])

    await provisioner(fake).request_account(REQUEST)

    (token_request,) = fake.token_requests
    assert "Authorization" not in token_request.headers
    assert form(token_request)["client_id"] == "cf-provision"
    assert form(token_request)["client_secret"] == "s3cret"


async def test_absent_name_and_locale_are_left_out() -> None:
    fake = FakeCyberdyneAuth()

    await provisioner(fake).request_account(ProvisioningRequest(email="leo@example.com"))

    assert json.loads(fake.provisions[0].content) == {"email": "leo@example.com"}
    assert provision_body(ProvisioningRequest("a@b.c", "", "")) == {"email": "a@b.c"}


@pytest.mark.parametrize(
    ("status", "raised"),
    [
        (429, ProvisioningRateLimited),
        (422, ProvisioningInvalidName),
        (200, ProvisioningUnavailable),
        (201, ProvisioningUnavailable),
        (400, ProvisioningUnavailable),
        (403, ProvisioningUnavailable),
        (500, ProvisioningUnavailable),
        (503, ProvisioningUnavailable),
    ],
)
async def test_every_answer_but_202_raises_what_it_means(status: int, raised: type) -> None:
    fake = FakeCyberdyneAuth(provision_statuses=[status])

    with pytest.raises(raised):
        await provisioner(fake).request_account(REQUEST)


async def test_the_token_is_reused_until_it_nearly_expires() -> None:
    fake, clock = FakeCyberdyneAuth(expires_in=300), Monotonic()
    adapter = provisioner(fake, clock)

    await adapter.request_account(REQUEST)
    clock.now += 200
    await adapter.request_account(REQUEST)
    assert len(fake.token_requests) == 1

    clock.now += 80  # within the 30-second margin of expiry
    await adapter.request_account(REQUEST)
    assert len(fake.token_requests) == 2
    assert fake.provisions[-1].headers["Authorization"] == "Bearer tok-2"


async def test_a_token_without_expires_in_is_still_renewed() -> None:
    fake, clock = FakeCyberdyneAuth(expires_in=None), Monotonic()
    adapter = provisioner(fake, clock)

    await adapter.request_account(REQUEST)
    clock.now += 60
    await adapter.request_account(REQUEST)

    assert len(fake.token_requests) == 2


async def test_a_401_gets_one_fresh_token_and_one_retry() -> None:
    fake = FakeCyberdyneAuth(provision_statuses=[401, 202])

    await provisioner(fake).request_account(REQUEST)

    assert len(fake.token_requests) == 2
    assert [r.headers["Authorization"] for r in fake.provisions] == [
        "Bearer tok-1",
        "Bearer tok-2",
    ]


async def test_a_second_401_is_unavailable_not_a_loop() -> None:
    fake = FakeCyberdyneAuth(provision_statuses=[401])

    with pytest.raises(ProvisioningUnavailable):
        await provisioner(fake).request_account(REQUEST)

    assert len(fake.provisions) == 2


async def test_a_refused_token_sends_nothing() -> None:
    fake = FakeCyberdyneAuth(token_status=401)

    with pytest.raises(ProvisioningUnavailable):
        await provisioner(fake).request_account(REQUEST)

    assert fake.provisions == []


async def test_discovery_naming_another_issuer_sends_nothing() -> None:
    fake = FakeCyberdyneAuth(discovered_issuer="https://evil.example.com")

    with pytest.raises(ProvisioningUnavailable):
        await provisioner(fake).request_account(REQUEST)

    assert fake.token_requests == []
    assert fake.provisions == []


async def test_no_answer_is_unavailable() -> None:
    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    adapter = CyberdyneAuthProvisioner(CLIENT, transport=httpx.MockTransport(unreachable))

    with pytest.raises(ProvisioningUnavailable):
        await adapter.request_account(REQUEST)


async def test_nothing_personal_or_secret_is_logged() -> None:
    fake = FakeCyberdyneAuth(provision_statuses=[422])

    with structlog.testing.capture_logs() as logs, pytest.raises(ProvisioningInvalidName):
        await provisioner(fake).request_account(REQUEST)

    written = json.dumps(logs, default=str)
    for value in ("leo@example.com", "Leo Araujo", "s3cret", "tok-1"):
        assert value not in written
    assert "s3cret" not in repr(CLIENT)


# --- configuration and wiring ----------------------------------------------

BASE = {
    "discord_token": "t",
    "discord_guild_id": 1,
    "database_url": "postgresql+asyncpg://x/y",
    "llm_api_key": "k",
}
CONFIGURED = {
    "account_provisioning_issuer": ISSUER + "/",
    "account_provisioning_client_id": "cf-provision",
    "account_provisioning_client_secret": "s3cret",
}


def test_without_the_client_there_is_no_adapter() -> None:
    settings = Settings.model_validate(BASE)
    assert build_account_provisioner(settings) is None


def test_empty_values_read_as_unset() -> None:
    """Compose passes `${VAR:-}`: an empty client is off, not half-configured."""
    settings = Settings.model_validate(
        {
            **BASE,
            "account_provisioning_issuer": "",
            "account_provisioning_client_id": "",
            "account_provisioning_client_secret": "",
        }
    )
    assert build_account_provisioner(settings) is None


@pytest.mark.parametrize(
    "extra",
    [
        {"account_provisioning_client_id": "cf-provision"},
        {"account_provisioning_client_secret": "s3cret"},
        {
            "account_provisioning_client_id": "cf-provision",
            "account_provisioning_client_secret": "",
        },
        {**CONFIGURED, "account_provisioning_issuer": ""},
        {**CONFIGURED, "account_provisioning_issuer": "http://auth.example.com"},
        {**CONFIGURED, "account_provisioning_issuer": "https://auth.example.com/?x=1"},
    ],
)
def test_a_partial_or_insecure_client_refuses_to_start(extra: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate({**BASE, **extra})


async def test_the_configured_client_goes_through_the_edges_transport() -> None:
    fake = FakeCyberdyneAuth()
    settings = Settings.model_validate({**BASE, **CONFIGURED})
    assert settings.account_provisioning_issuer == ISSUER

    adapter = build_account_provisioner(settings, fake.transport())
    assert adapter is not None
    await adapter.request_account(REQUEST)

    assert len(fake.provisions) == 1
    assert "s3cret" not in repr(settings)
