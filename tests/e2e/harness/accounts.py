"""The CyberdyneAuth provisioning endpoint, faked at the `AccountProvisioner` port.

The approved contract answers 202 whatever it did -- created, already existed,
throttled -- so this fake accepts everything and records what it was sent.
`refuse_next` makes the next call fail the way a 429 or an outage would.
"""

from __future__ import annotations

from chatmemory.ports.accounts import ProvisioningFailed, ProvisioningRequest


class FakeProvisioner:
    """Always 202; records every request, so a scenario can read what left."""

    def __init__(self) -> None:
        self.requests: list[ProvisioningRequest] = []
        self._refusal: ProvisioningFailed | None = None

    def refuse_next(self, refusal: ProvisioningFailed) -> None:
        self._refusal = refusal

    async def request_account(self, request: ProvisioningRequest) -> None:
        refusal, self._refusal = self._refusal, None
        if refusal is not None:
            raise refusal
        self.requests.append(request)
