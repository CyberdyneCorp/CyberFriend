## Why

Every wiring test in this repository reads source: it parses `main()` and
asserts that a call is there. That catches a keyword left out, but not a graph
that is built and then behaves differently from the one a deployment runs. The
failure this project keeps having is a capability that was built, tested, and
silently never reached -- and a test that builds its own copy of the object
graph cannot see that failure, because its copy is not the production one.

An end-to-end harness needs to build the *exact* production object graph with
fakes only where the process touches the network: the chat and summary
models, the embedding endpoint, the database engine, outbound HTTP and the
clock. Today those are constructed deep inside `build_answer_stack` and
`main`, so there is nowhere to hand a fake in without rebuilding the graph.

## What Changes

- **An `Edges` value** in `composition.py` names everything the graph reaches
  the outside world through: `chat`, `summary_chat`, `embeddings`, `engine`,
  `http_transport` (optional) and `clock`. `Edges.production(settings)` builds
  them exactly as before.
- **`build_answer_stack(..., edges=...)`** takes its chat handle, embedding
  client and engine from `edges` instead of constructing them. The embedding
  width check still runs, against whatever `edges` holds.
- **`assemble(settings, edges) -> Process`** in `entrypoints/bot.py` does
  everything `main()` did before it connected the gateway: the answer stack,
  the live scope and its first refresh, conversation memory, personal facts
  and `build_bot` with every collaborator. `main()` becomes
  `assemble(settings, Edges.production(settings))` followed by the same
  startup.
- **Wiring tests move with the code**: the AST checks that read `main()` for
  graph construction now read `assemble()`, and new tests assert that `main`
  assembles over `Edges.production`, that `assemble` hands `build_bot` every
  collaborator it accepts, and that nothing between the edges constructs an
  edge of its own.

Non-goals (later changes in this series):

- **Threading `http_transport` and `clock` into adapters.** This change only
  carries the fields; production leaves them at the defaults adapters already
  use.
- **The harness itself** -- fake Discord, fake model, fixtures.

## Capabilities

### New Capabilities

- `testing`: how production and tests build the bot process, and what a test
  may replace.

### Modified Capabilities

None. No behaviour changes in production.

## Risk

The risk is a refactor that changes production while claiming not to. It is
bounded by construction order: `Edges.production` builds the chat handle
first, so a model that cannot serve still fails before a pool is opened or an
embedding call is spent, and the embedding width is still checked before the
graph is returned. The only construction that moved earlier is the summary
model handle, which is a client object with no network call.
