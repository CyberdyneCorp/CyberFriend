## 1. CI gate for the current console

- [x] 1.1 Add a `console` job to `.github/workflows/ci.yml`: `npm ci`, `npm test`, `npm run build`
- [x] 1.2 Widen `no-browser-storage.test.ts` to `.svelte` and `.svelte.ts`, and strip HTML comments
- [x] 1.3 Test: a `.svelte` fixture that uses `localStorage` fails the guard

Lint (with the cognitive-complexity limit) is not added to the React console,
which 3.5 deletes; it arrives with the Svelte tree in 2.1/2.7 and covers
`console/` from the swap on (3.5).

## 2. Svelte foundation (`console-svelte/`)

- [x] 2.1 Vite + Svelte 5 + svelte-check + vitest + eslint (svelte, sonarjs cognitive-complexity at 12)
- [x] 2.2 Port `domain/` and its tests unchanged
- [x] 2.2a Port `no-browser-storage.test.ts` and `fixtures/storage-guard/` into `console-svelte/`, so the Svelte sources are scanned during the port
- [x] 2.3 `services/http.ts` and `services/adminApi.ts`, with the ported `client.test.ts`
- [x] 2.4 `Resource` and `Action` view-models, with node tests
- [x] 2.5 Hash router and route table (explicit `minRole` on every route), with unknown path -> `#/status`
- [x] 2.6 `architecture.test.ts` enforcing the import rule
- [x] 2.7 CI builds, lints and tests `console-svelte/`

SignIn and Status are ported in `console-svelte/` as the template screen, with
an end-to-end test against the stub; 3.1 stays open for Settings and Audit.

`architecture.test.ts` reads each file with the TypeScript parser and the
Svelte compiler rather than a pattern, holds `main.ts` to the network rule and
the domain to "no packages", and the shell's role filter has its own component
test. The Svelte client resolves `api` against the page it was loaded from, so
behind a path prefix it calls `<prefix>/api` (the React client still calls the
origin's `/api` until 3.5).

## 3. Screens and the swap

- [x] 3.1 SignIn, Status, Settings (+ SettingsTable), Audit
- [x] 3.2 Channels, Tokens, Retention
- [x] 3.3 Federation (ServersVM, AllowlistVM, TypedConfirmVM, ServerCard, TypedConfirm)
- [x] 3.4 Port `console.e2e.test.tsx` to @testing-library/svelte against the same stub
- [x] 3.5 Replace `console/` with the Svelte tree; delete React dependencies; the CI `console` job gains the lint step and 2.7's `console-svelte/` job is folded into it
- [x] 3.6 Rewrite `docs/admin-console.md`; drop the stale "bundle is not built" notes
- [x] 3.7 Add `chatmemory.entrypoints.admin` to the Dockerfile import smoke check

The swap moved `console-svelte/` to `console/` (so the Dockerfile's path did
not change) and deleted the React tree. The two-click removal is a
`Confirmation` view-model rather than component state, and `domain/allowlist.ts`
holds the pure allowlist-key rule the federation view-models and `ServerCard`
share. The stub API gained the write routes the other screens' scenarios need
(channels, settings, tokens, opt-outs) and records each request.
