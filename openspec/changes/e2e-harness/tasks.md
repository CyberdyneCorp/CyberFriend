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

## 4. Later in the series

- [ ] 4.1 End-to-end harness over `assemble` with fakes at the edges
