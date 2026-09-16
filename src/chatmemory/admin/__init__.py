"""The operator-facing console: who is configuring the agent, and what changed.

Two modules, and the split between them is the whole point of the capability:

*   `auth` decides **who is acting**, from the credential and from nothing
    else. A request has no field that can name an operator, so there is
    nothing for a forged one to aim at.
*   `audit` records **what they changed**, append-only, refused attempts
    included.

Neither knows anything about the corpus. The console configures the agent; it
is not a second way into private channels, and giving it one would bypass
every viewer check the rest of the system is built around.
"""
