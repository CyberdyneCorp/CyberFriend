"""What the console is wired to. One value, so nothing can be half-wired.

Every dependency the handlers have, as ports, in one frozen record. The shape
is deliberate: this project's recurring defect is a feature that is built,
tested and then connected to nothing, and a bag of optional constructor
arguments is how that happens -- one is forgotten, the surface still starts,
and the screen that needed it is quietly inert.

So there are no defaults here. A console that cannot record a change, or
cannot probe a server, must fail to be constructed rather than run with that
screen silently broken.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from chatmemory.adapters.mcp_client.config import ServerConfig
from chatmemory.adapters.mcp_client.registry import ServerDiscovery
from chatmemory.admin.audit import ChangeRecordStore
from chatmemory.admin.handlers.queries import (
    ChannelDirectory,
    CorpusStatus,
    OptOutDirectory,
)
from chatmemory.app.configuration import ConfigurationEditor, RuntimeConfiguration
from chatmemory.app.optout import OptOutService
from chatmemory.app.tokens import TokenDirectory

ServerProbe = Callable[[ServerConfig], Awaitable[ServerDiscovery]]
"""Ask one federated server what it offers, without joining it to anything.

A function rather than an object because there is no state worth keeping
between probes: the answer is only true for as long as the server stays up,
which is exactly why the allowlist is re-checked at startup as well.
"""


@dataclass(frozen=True, slots=True)
class AdminServices:
    """The console's dependencies, as ports.

    Note what is absent: no Discord client, no model client, no search
    backend. The console configures the agent and has no route to the corpus'
    content or to the credentials the agent runs on, and the way that is
    guaranteed is that it is never handed either.
    """

    #: The settings in force, re-read on a cadence, with provenance.
    configuration: RuntimeConfiguration
    #: The only supported way to change a stored setting. Validates first.
    editor: ConfigurationEditor
    #: Who changed what. Append-only; the console can read it and add to it.
    changes: ChangeRecordStore
    #: Counts and timings for the status screen.
    status: CorpusStatus
    #: What the corpus records about each channel.
    channels: ChannelDirectory
    #: Who has withdrawn from the corpus.
    optout_directory: OptOutDirectory
    #: Recording a withdrawal, and the purge that has to go with it.
    optouts: OptOutService
    #: MCP credentials -- the *agent's* readers, not console operators.
    #: A directory, not a store: the console reviews and revokes them and
    #: cannot mint one. Minting names a Discord account, and the credential
    #: minted reads everything that account can see, so a console able to do
    #: it would be a route to the corpus around every other guarantee here.
    #: Issuance stays in `python -m chatmemory.mcp.issue_token`, which needs
    #: the agent's own environment and a shell on the host.
    mcp_tokens: TokenDirectory
    #: Reachability and offered tools, asked of a server before it is stored.
    probe: ServerProbe
