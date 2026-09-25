## Context

The console is a static bundle served by the Starlette admin API
(`src/chatmemory/admin/server.py` mounts `StaticFiles(html=True)` from
`ADMIN_CONSOLE_DIR`). It talks only to `/api/*` on the same origin. The React
code already separates a pure `domain/` from the rest, and has two state hooks
(`useResource`, `useAction`) that are view-models in all but name. The port
keeps both ideas and makes the boundary explicit.

## Goals / Non-Goals

**Goals:**

- Screen parity: an operator who used the React console notices nothing except
  the look.
- State and decisions live in view-models that tests can drive without a DOM.
- Cognitive complexity per function at 8-12, enforced by lint.
- The console's own tests run in CI.

**Non-Goals:**

- Changing authentication or the API.
- SSR, a Node server, or SvelteKit routing.

## Decisions

### Plain Svelte 5 + Vite, not SvelteKit

The server serves static files and nothing else, and the next change puts the
OIDC flow on the Python side (a BFF). SvelteKit's SSR, load functions and
file-system routing would duplicate or fight that. Plain Svelte with
`@sveltejs/vite-plugin-svelte` builds to the same `dist/` the Dockerfile
already copies.

### Hash router, about thirty lines

A `$state` fed by `hashchange` plus a route table
`[{ path, title, view, minRole }]`. The hash keeps the server free of SPA
rewrites, as `App.tsx` 1-9 chose for React. `minRole` is unused until the login
change and defaults to `operator`. Unknown paths go to `#/status`.

Alternative considered: `svelte-spa-router`. Rejected as a dependency for
something this small.

### Layers and the import rule

```
views/       -> viewmodels/, domain/
viewmodels/  -> services/, domain/
services/    -> domain/ (types only)
domain/      -> nothing
```

- `services/http.ts` is the only file that calls `fetch`. It keeps today's
  behaviour: `Authorization: Bearer`, `credentials: "omit"`, per-segment path
  encoding, and a 401 that signs out with one fixed message.
- `services/adminApi.ts` has the same surface as `api/client.ts` 124-171 and
  still has no mint or issue call.
- `viewmodels/resource.svelte.ts` (`Resource<T>`: data, error, loading,
  reload) replaces `useResource`. `viewmodels/action.svelte.ts` (`Action`: run,
  busy, error, note, clear) replaces `useAction`, keeping the note because a
  successful call can carry a warning.
- View-models take their services through the constructor, so a test writes
  `new ChannelsVM(fakeApi)`.
- FederationScreen is split into `ServersVM`, `AllowlistVM` and
  `TypedConfirmVM` to stay inside the complexity target.

`architecture.test.ts` scans sources and fails on an import that breaks the
rule, in the same style as the storage guard.

### Guards carried over, and widened

`no-browser-storage.test.ts` scans only `.ts`/`.tsx` today (line 33). A Svelte
port with the guard unchanged would silently stop checking the files that
matter. The guard now scans `.ts`, `.svelte` and `.svelte.ts`, strips HTML
comments as well as JS comments, and keeps every existing rule (no
localStorage, sessionStorage, indexedDB, document.cookie or window.name, no
token in a query parameter, and the token read in exactly one file:
`services/http.ts`).

### Tests

- Node environment: domain and view-model tests.
- jsdom + @testing-library/svelte: component tests and the port of
  `console.e2e.test.tsx`, still against the `node:http` stub in
  `test/fixtures.ts`.
- The same scenarios as today: sign-in refused vs accepted, mutating vs
  read-only badges, the typed-confirm gate, channel readability, settings
  provenance badges, audit refusals.

### Landing order

1. A CI job for the current React console plus the widened guard. This closes
   the CI gap before anything moves.
2. The Svelte foundation (domain, services, view-models, router, tests) in
   `console-svelte/`, built and tested in CI but not shipped.
3. The screens, then the swap: `console-svelte/` replaces `console/` in one
   PR, so the Dockerfile path never changes and there is never a half-React
   bundle in the image.

## Risks / Trade-offs

- [A regression in a safety gesture, such as the typed confirm] -> The e2e
  scenarios port first and must pass against the Svelte tree before the swap.
- [The widened guard finds nothing because the Svelte compiler rewrites
  sources] -> The guard scans source files, not the build output.
- [Two console trees for a few days] -> Only `console/` is copied into the
  image. `console-svelte/` lives only until the swap PR.
