## ADDED Requirements

### Requirement: One assembly function builds the bot process

The bot process's object graph SHALL be built by a single assembly function
that takes the settings and the process's network edges -- the chat model,
the summary model, the embedding client, the database engine, an optional
HTTP transport and a clock. The production entrypoint and end-to-end tests
SHALL both build the process through that function, and nothing inside it
SHALL construct a chat model, summary model, embedding client or database
engine of its own. Outbound HTTP made by the federation layer and the tracer
is not yet routed through the edges; threading the HTTP transport into those
adapters is a later change.

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
