## ADDED Requirements

### Requirement: Per-server service authentication

The system SHALL let an operator give a configured federated server a
CyberdyneAuth client (issuer, client id, client secret, and optionally an
audience and a scope). For such a server it SHALL obtain a bearer token with
the `client_credentials` grant from the issuer's token endpoint. It SHALL
send the token as `Authorization: Bearer` on that server's MCP requests and
SHALL NOT send it on any other request.

#### Scenario: Token sent to its server
- GIVEN a server configured with a service credential
- WHEN the system connects to it or calls one of its tools
- THEN each MCP request to that server SHALL carry the bearer token obtained for that credential

#### Scenario: Token not sent elsewhere
- GIVEN two configured servers of which only one has a service credential
- WHEN the system calls a tool on the other server
- THEN that request SHALL carry no token obtained for the first server

#### Scenario: Token reused until it expires
- WHEN a token has been obtained and has not reached its expiry margin
- THEN the system SHALL reuse it rather than request another

#### Scenario: Token refreshed
- WHEN the cached token is within its expiry margin, or the server answers `401`
- THEN the system SHALL obtain a new token and, for a `401`, retry the request once with it

#### Scenario: Issuer unavailable
- WHEN a token cannot be obtained
- THEN the server SHALL be treated as unavailable, as for any transport failure
- AND the other servers SHALL keep working

#### Scenario: Secrets are not logged
- WHEN a token is obtained, refused or used
- THEN neither the client secret nor the token SHALL appear in logs

### Requirement: Service credentials are validated at startup

A service credential that cannot be honoured safely SHALL be a startup
configuration error, not a runtime failure.

#### Scenario: Credential for an unconfigured server
- WHEN a service credential names a server that is not configured
- THEN startup SHALL report a federation configuration error

#### Scenario: Incomplete credential
- WHEN a server's credential has some but not all of issuer, client id and client secret
- THEN startup SHALL report a federation configuration error

#### Scenario: Plain-text transport
- WHEN a credentialled server's target or its issuer is not HTTPS, other than on localhost
- THEN startup SHALL report a federation configuration error

### Requirement: Personal tools are answered only in direct messages

A federated tool whose name starts with `my_` SHALL be invocable only for an
answer delivered to the asker's direct messages. Anywhere else the invocation
SHALL be refused before anything is sent to the server.

#### Scenario: Personal tool in a channel
- WHEN a run answering in a server channel, or in an ephemeral reply, invokes a `my_` tool
- THEN the invocation SHALL be refused
- AND the server SHALL NOT be called

#### Scenario: Personal tool in a DM
- WHEN a run answering in the asker's direct messages invokes a registered `my_` tool
- THEN the personal rule SHALL NOT refuse it

### Requirement: Personal results are withheld outside direct messages

A federated result that starts with `[personal]`, or whose structured content
has `visibility` equal to `personal`, SHALL NOT be used in an answer that is not
delivered to the asker's direct messages, whichever tool returned it.

#### Scenario: Personal result in a channel
- WHEN a tool not named `my_` returns a personal result to a run answering in a channel
- THEN the result SHALL be withheld from the run
- AND the audit SHALL record the invocation as refused

### Requirement: External financial sources are neither remembered nor public

Results from a federated financial server, public (`intel_`) or personal
(`my_`), SHALL be cited under that server's name as an external system. A
conversation turn that rests on them SHALL NOT be remembered in channel
memory. `intel_` results SHALL NOT count as a public source.

#### Scenario: Turn resting on a federated result
- WHEN an answer cites a result from a federated server that is not a public source
- THEN the turn SHALL NOT be stored in conversation memory

#### Scenario: Intel result cited
- WHEN an answer uses an `intel_` result
- THEN its citation SHALL name the federated server as the source system
