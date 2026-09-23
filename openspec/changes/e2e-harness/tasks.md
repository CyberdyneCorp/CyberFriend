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
- [x] 2.5 Test: nothing between the edges builds an edge of its own
- [x] 2.6 Test: `assemble` builds the whole process over fake edges

## 3. Later in the series

- [ ] 3.1 Thread `http_transport` into the HTTP adapters
- [ ] 3.2 Thread `clock` into the loops and services that read the time
- [ ] 3.3 End-to-end harness over `assemble` with fakes at the edges
