## 1. The seam

- [x] 1.1 `Edges` dataclass with `chat`, `summary_chat`, `embeddings`, `engine`, `http_transport`, `clock`
- [x] 1.2 `Edges.production(settings)` reproducing today's construction and order
- [x] 1.3 `build_answer_stack(..., edges=...)` takes its edges instead of building them
- [x] 1.4 `ToolCapableChat` port so the tool proposer accepts any chat edge
- [x] 1.5 `assemble(settings, edges) -> Process` holding everything `main` built
- [x] 1.6 `main()` assembles over `Edges.production` and starts as before

## 2. Tests

- [x] 2.1 Move AST wiring checks from `main()` to `assemble()`
- [x] 2.2 Test: `main` calls `assemble` with `Edges.production`
- [x] 2.3 Test: `assemble` passes every `build_bot` collaborator
- [x] 2.4 Test: `Edges.production` builds the same component types as before
- [x] 2.5 Test: nothing between the edges builds a model, embedding client or engine of its own
- [x] 2.6 Test: `assemble` builds the whole process over fake edges

## 3. HTTP transport and clock

- [x] 3.1 `transport` on `ChainToolsConfig`, `MarketToolsConfig` and `WebToolsConfig`, passed to every `httpx.AsyncClient` their providers open
- [x] 3.2 `transport` on the Langfuse tracer; also accepted by the attachment fetcher, the Drive and Notion clients and the trace deleter, which no process built over `Edges` constructs yet
- [x] 3.3 `build_answer_stack` hands `edges.http_transport` to `build_federation` (and on to each tool config) and to `build_tracer`
- [x] 3.4 `clock` on `ReasoningAnswerService`, `ModelPlanner` and `ModelSynthesizer`: the time route and the prompt's clock notice read it
- [x] 3.5 `edges.clock` reaches the answer service, catch-up, and the scheduled-task and notification loops `main` starts
- [x] 3.6 Test: no `httpx.AsyncClient(` under chain, market, web, documents or tracing is opened without `transport=`
- [x] 3.7 Test: a mock transport handed to `build_federation` receives the `eth_getBalance` batch of a wallet lookup
- [x] 3.8 Test: a fixed clock in `Edges` is the time the time route answers; the loops read the clock they are given
- [x] 3.9 Test: each web and market provider, the wallet's USD price lookup and the trace export reach a mock transport
- [x] 3.10 Test: the planner, synthesiser and catch-up read the clock they are built on, and `assemble` hands catch-up `edges.clock`

## 4. The harness

- [x] 4.1 Fake Discord wire: guild, channels and members from gateway payloads through the client's `ConnectionState`; `FakeHTTP` recording messages and the command-sync payloads; a fake webhook adapter for interaction responses and followups; the real `setup_hook`
- [x] 4.2 `ScriptedChat` (with `tool_caller`) and `HashEmbeddings` behind the existing ports; `FakeWeb` answering by host, refusing unknown hosts
- [x] 4.3 `Conversation` / `Turn` fixture API: `say`, `mention_only`, `slash`; `text`, `searched`, `hosts`, `schemas`, `edge()`, `assert_language`; memory and fact rows by SQL; every `/name` said checked against what is offered there
- [x] 4.4 Real Postgres: `TEST_DATABASE_URL` with a testcontainers pgvector fallback, `E2E_REQUIRE_DB=1` turning a skip into a failure, a TRUNCATE per test
- [x] 4.5 Network canary: httpx's real transports raise inside the suite
- [x] 4.6 discord.py pinned `~=2.7.1` in the dev extras; a canary test naming each internal the wire uses
- [x] 4.7 A 60-second budget for the suite, enforced
- [x] 4.8 CI step: `pytest tests/e2e` with `E2E_REQUIRE_DB=1` after the migration
- [x] 4.9 Docs: end-to-end tests in `docs/operations.md`, including the rule that a production fix replays its transcript as a scenario

## 5. Anchor scenarios

- [x] 5.1 S5: a positions follow-up recalls the address turn one stored through the real provider (row read by SQL, never seeded)
- [x] 5.2 S7: `/forget` typed in a DM points at a command offered there, and `/forget everywhere` run from the DM erases turns and facts
- [x] 5.3 S8: global payload holds the personal commands with contexts `[0,1]` and integration types `[0]`; the guild payload holds `/index` and `/unindex`; a committed snapshot of both
- [x] 5.4 S12: fixed replies to Portuguese questions; `NOTHING_FOUND` localised, English-only constants as strict xfails naming the constant
- [x] 5.5 Corpus control: a channel question searches the archive and cites it
- [x] 5.6 Privacy: a channel the asker cannot read never reaches the answer or the model, in a DM or a channel; the withheld notice goes to the asker by DM
- [x] 5.7 Mutation check: breaking chain-turn provenance, DM command contexts or one Portuguese fixed reply turns S5, S7/S8 or S12 red

## 6. Ingest ask extraction

- [x] 6.1 `build_ask_pipeline(..., extractor=...)`: the extractor is injectable, and production leaves it unset
- [x] 6.2 `ObligationAnswerService` built on `edges.clock`; wiring test
- [x] 6.3 Harness `Ingest`: `to_message` -> `IngestService.capture` -> `ExtractionWorker.submit`, flushed by `E2EBot.extract_asks`; `ChatAskExtractor` sends the production prompt to `ScriptedChat` as `ask_extraction`, and `ScriptedChat.script_asks` scripts the reply
- [x] 6.4 `FakeDiscord.chatter`: a channel message not addressed to the bot, its snowflake minted from when it was said
- [x] 6.5 Scenario: "what was asked of me this week" cites the source message; an older ask and a #leadership ask are absent; unaddressed chatter costs no extraction call

## 7. Later in the series

- [ ] 7.1 Remaining production-failure scenarios (S1-S4, S6, S9-S11) with recorded fixtures
- [ ] 7.2 Retire the hand-built stand-ins the scenarios replace
- [ ] 7.3 A stratified slice of the labelled routing set end to end
