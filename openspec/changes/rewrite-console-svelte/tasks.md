## 1. CI gate for the current console

- [ ] 1.1 Add a `console` job to `.github/workflows/ci.yml`: `npm ci`, `npm test`, `npm run build`
- [ ] 1.2 Widen `no-browser-storage.test.ts` to `.svelte` and `.svelte.ts`, and strip HTML comments
- [ ] 1.3 Test: a `.svelte` fixture that uses `localStorage` fails the guard

## 2. Svelte foundation (`console-svelte/`)

- [ ] 2.1 Vite + Svelte 5 + svelte-check + vitest + eslint (svelte, sonarjs cognitive-complexity at 12)
- [ ] 2.2 Port `domain/` and its tests unchanged
- [ ] 2.3 `services/http.ts` and `services/adminApi.ts`, with the ported `client.test.ts`
- [ ] 2.4 `Resource` and `Action` view-models, with node tests
- [ ] 2.5 Hash router and route table, with unknown path -> `#/status`
- [ ] 2.6 `architecture.test.ts` enforcing the import rule
- [ ] 2.7 CI builds, lints and tests `console-svelte/`

## 3. Screens and the swap

- [ ] 3.1 SignIn, Status, Settings (+ SettingsTable), Audit
- [ ] 3.2 Channels, Tokens, Retention
- [ ] 3.3 Federation (ServersVM, AllowlistVM, TypedConfirmVM, ServerCard, TypedConfirm)
- [ ] 3.4 Port `console.e2e.test.tsx` to @testing-library/svelte against the same stub
- [ ] 3.5 Replace `console/` with the Svelte tree; delete React dependencies
- [ ] 3.6 Rewrite `docs/admin-console.md`; drop the stale "bundle is not built" notes
- [ ] 3.7 Add `chatmemory.entrypoints.admin` to the Dockerfile import smoke check
