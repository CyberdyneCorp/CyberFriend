## ADDED Requirements

### Requirement: One assembly function builds the bot process

The bot process's object graph SHALL be built by a single assembly function
that takes the settings and the process's network edges -- the chat model,
the summary model, the embedding client, the database engine, an optional
HTTP transport and a clock. The production entrypoint and end-to-end tests
SHALL both build the process through that function, and nothing inside it
SHALL construct a chat model, summary model, embedding client or database
engine of its own.

#### Scenario: Production builds its edges from settings
- WHEN the bot process starts
- THEN it SHALL build its edges from settings and hand them to the assembly
  function
- AND the edges SHALL be the same components the process used before the
  assembly function existed

#### Scenario: A test replaces only the edges
- WHEN a test calls the assembly function with fake edges
- THEN the returned process SHALL hold the object graph production runs,
  with its chat model, summary model, embedding client and database engine
  being those fakes

#### Scenario: Assembly starts nothing
- WHEN the assembly function returns
- THEN no gateway connection SHALL have been opened and no background loop
  SHALL have been started

### Requirement: Boot checks run against the edges

The embedding width check SHALL run against the embedding client the edges
hold, and the chat model capability check SHALL run when production edges are
built, so a process that the assembly function returns is one whose model and
corpus agree with its configuration.

#### Scenario: A fake embedding client of the wrong width
- WHEN the edges hold an embedding client whose vectors are narrower than the
  configured width
- THEN assembly SHALL fail with a configuration error naming both widths

### Requirement: The assembly function passes every bot collaborator

The assembly function SHALL hand the bot builder every collaborator it
accepts, so a feature cannot be built and reached by nothing.

#### Scenario: A collaborator left out
- WHEN the bot builder accepts a collaborator the assembly function does not
  pass
- THEN the wiring tests SHALL fail naming it

### Requirement: Outbound HTTP goes through the edges' transport

Every HTTP client opened by the federation's local providers (web, market and
wallet) and the trace exporter SHALL be opened with the transport the edges
hold. An edges value with no transport SHALL leave every such client on
httpx's own network transport, which is the behaviour before the transport
was threaded. Adapters that no process built over the edges constructs yet
(the document fetchers and the trace deleter) SHALL still accept a transport,
so they can join the seam without opening a client of their own.

#### Scenario: A mock transport receives a wallet lookup
- WHEN the edges hold a mock HTTP transport and a person's wallet lookup is
  invoked through the federation
- THEN the balance request and the USD price lookup SHALL reach the mock
  transport and not the network

#### Scenario: A mock transport receives every local provider's call
- WHEN a mock HTTP transport is handed to the federation and a web or market
  tool is invoked through it
- THEN that provider's request SHALL reach the mock transport

#### Scenario: A mock transport receives the trace export
- WHEN a mock HTTP transport is handed to the tracer and a run is traced
- THEN the export SHALL reach the mock transport

#### Scenario: A client opened without the transport
- WHEN an adapter under chain, market, web, documents or tracing opens an
  HTTP client without passing a transport
- THEN the wiring tests SHALL fail naming the file and line

### Requirement: Answers and loops read the edges' clock

The time route, the clock notice in answer prompts, catch-up, and the
scheduled-task and notification loops SHALL read the current time from the
clock the edges hold. Production edges SHALL hold the UTC wall clock.

#### Scenario: A fixed clock answers the time route
- WHEN the edges hold a clock fixed at a moment and a person asks what the
  date is
- THEN the answer SHALL state that moment

#### Scenario: A fixed clock is the time the prompts state
- WHEN the answer service or catch-up is built on a fixed clock
- THEN the planner's and synthesiser's prompts SHALL state that moment, and a
  catch-up period SHALL be measured from it

### Requirement: End-to-end scenarios drive the assembled process through the wire

End-to-end tests SHALL build the bot process with the assembly function over
fake edges and a real migrated database, and SHALL drive it only through a
fake Discord wire: gateway-shaped messages and slash-command interactions in,
and the REST and interaction-webhook calls Discord would receive out. The bot
client's own command registration SHALL run and its sync payloads SHALL be
recorded. Scenario assertions SHALL be limited to what is observable at the
edges -- what Discord received, whether the corpus was searched, which hosts
were reached, which model stages ran, and memory and fact rows read by SQL.

#### Scenario: A scenario reaches an unscripted host
- WHEN a scenario makes an HTTP request to a host no fixture answers for, or
  any HTTP client not built over the edges' transport opens a connection
- THEN the scenario SHALL fail naming the host

#### Scenario: No database is available
- WHEN the end-to-end database is unreachable and the run requires one
- THEN the suite SHALL fail rather than skip

#### Scenario: The bot names a command that is not offered there
- WHEN a reply tells a person to run a slash command that Discord would not
  list where the reply was sent
- THEN the scenario SHALL fail naming the command

#### Scenario: A slash command invoked where it is not offered
- WHEN a scenario invokes a slash command in a DM or a guild where the synced
  registration does not offer it
- THEN the invocation SHALL be refused before the command runs

#### Scenario: A known-open defect is fixed
- WHEN a scenario marked as a known-open defect starts passing
- THEN the suite SHALL fail until the marker is removed
