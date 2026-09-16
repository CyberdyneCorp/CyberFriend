# The operator console

A TypeScript single-page app that talks to the admin API and to nothing else.
It is static files: no server of its own, no credentials of its own, no build
step at runtime. The admin API serves it from the same origin it serves `/api`
from, which is what lets the bundle use a relative API root and hold the
operator's token in memory.

Source is in `console/`. Build output is `console/dist/`, which is
`.gitignore`d — it is built during the image build, not committed.

## Build and run

```bash
cd console
npm install          # once
npm run build        # type-checks, then emits console/dist/
npm test             # unit tests, including the browser-storage guard
```

Development against a running admin API:

```bash
cd console
ADMIN_API_ORIGIN=http://127.0.0.1:8083 npm run dev   # http://localhost:5174
```

`npm run dev` proxies `/api` to `ADMIN_API_ORIGIN` so the browser sees one
origin in development as it does in production. The default origin is
`http://127.0.0.1:8083`; set it to wherever the admin service is listening.

## How it is served — the wiring that makes it reachable

The bundle is inert until something serves it. The admin service is that
something, and this is the whole chain, from the process an operator starts to
the file a browser gets:

```
python -m chatmemory.entrypoints.admin
  └─ entrypoints/admin.py : main()
       └─ build(os.environ)
            ├─ console_dir(environ)          reads ADMIN_CONSOLE_DIR
            └─ admin/server.py : build_app(services, tokens, console_dir)
                 ├─ Route("/health"), Route("/ready")        open
                 ├─ api_routes(services)                     under /api
                 ├─ Route("/api/{rest:path}") -> no_corpus   refuses the rest
                 ├─ _console(console_dir)
                 │    └─ Mount("/", StaticFiles(console_dir, html=True))
                 └─ AdminAuthMiddleware(protected_prefix="/api")
```

So: **set `ADMIN_CONSOLE_DIR` to `console/dist`**. That is the one piece of
configuration that connects this bundle to the process. Without it the service
still runs and `_console` serves a page that says the interface is missing and
how to build it — deliberately, because a deploy that forgot to copy the
bundle in would otherwise look like a broken API.

The service listens on `ADMIN_PORT`, default **8083**, which is why that is the
dev proxy's default too.

Three properties of that chain are worth knowing:

- `StaticFiles(..., html=True)` mounted at `/` answers `/` with `index.html`
  and `/assets/*` with the built files. It is mounted last, so it claims only
  what no route above it did.
- `AdminAuthMiddleware(protected_prefix="/api")` leaves the bundle open. It is
  code, not configuration; it holds no credential, stores nothing, and is inert
  until a token is typed into it. A browser could not send a bearer token for a
  `<script src>` in any case.
  The prefix is matched against the *routed* path — `scope["path"]` with
  `root_path` stripped, which is what Starlette's router matches on — so the
  guard and the router always agree about which request is an API call. That
  matters for the mounted shapes below: served under `/console/`, a proxy sets
  `root_path`, the router still dispatches `/api/settings`, and a guard reading
  the raw path would have waved every read through unauthenticated.
- Anything under `/api` that no handler claimed is refused with "this console
  configures the agent; it returns no message, document or ask content", not a
  404. The console never asks for such a path; the refusal is there for
  whatever else does.

Two choices in the build exist to keep that list short:

- **`base: "./"`.** Asset URLs are relative, so the bundle works whether it is
  mounted at `/`, at `/console/`, or anywhere else. An absolute base is a
  second thing that has to be kept in step with the server, and it fails as a
  blank page with two 404s in the network tab.
- **Hash routing.** `#/federation`, not `/federation`. A path router would need
  the API to rewrite every unknown path to `index.html`, and a missing rewrite
  fails on *refresh* — days after the deploy that caused it, on somebody
  else's screen.

### In the image — not yet wired

`docker-compose.yml` already sets `ADMIN_CONSOLE_DIR=/app/console` for the
`admin` service. **Nothing puts the bundle there.** The `Dockerfile` has no
Node stage and copies no `console/dist`, so a deployed admin service finds no
directory, logs `admin.console_bundle_missing`, and serves the page that says
the interface is absent. The API works; the console is not there.

Two stanzas in `Dockerfile` close it — a build stage, and one `COPY` into the
existing image:

```dockerfile
FROM node:22-slim AS console
WORKDIR /console
# Manifests first, so application edits do not invalidate the install layer.
COPY console/package.json console/package-lock.json ./
RUN npm ci
COPY console/ ./
RUN npm run build          # type-checks, then emits /console/dist

# …in the Python image, after the source is installed:
COPY --from=console /console/dist /app/console
```

`ADMIN_CONSOLE_DIR=/app/console` then points at a real directory. Node is
needed at build time only; nothing in the running image depends on it.

That change belongs to whoever owns `Dockerfile`; this console was built and
verified against a locally running service instead (below).

### Verified, not assumed

This chain was exercised rather than read. With migrations applied to a scratch
database, a credential issued by
`python -m chatmemory.admin.issue_token issue <operator>`, and

```bash
DATABASE_URL=… ADMIN_PORT=8199 \
  ADMIN_CONSOLE_DIR=$PWD/console/dist \
  python -m chatmemory.entrypoints.admin
```

the service logged `admin.console_bundle` naming that directory, `GET /`
returned this `index.html` from uvicorn, both hashed assets returned 200,
`/api/status` returned 401 without a credential and the real status payload
with one — and the console, rendered against that running service, drew every
screen from it: settings with their real provenance, a channel the bot cannot
read with the API's own evidence line, and the audit showing the CLI's own
credential issue as an escalation.

## What is on each screen

| Screen | What it answers |
| --- | --- |
| Status | Is it healthy, is ingestion keeping up, how large is the embedding backlog, what changed recently. |
| Federation | Which servers the agent may reach, whether they answered a probe, what tools they offer, and which of those tools are allowed. |
| Channels | What is indexed, and whether the bot can actually read each one. Adding or removing a channel re-reads stored scope first, so it never undoes an `/index` or `/unindex` made from Discord since the console last refreshed; if stored scope cannot be read the edit is refused (503) and nothing is written. |
| Retention | How long anything is kept, and who has opted out. |
| Settings | Every setting, its value, and **where that value came from**. |
| Tokens | Review and revoke MCP credentials. Issuing one is not possible from here; see below. |
| Audit | Who changed what, when, from what to what. |

## The four rules this interface is built around

### The token is in memory, and nowhere else

The operator's token lives in a module variable in `src/auth/session.ts`. Not
`localStorage`, not `sessionStorage`, not a cookie. This console can add a
federated server and enable a tool that changes state somewhere else, so its
credential is a grant of new capability to the agent — and a credential in
browser storage outlives the tab, the session and the attention of the person
who pasted it.

The cost is that a reload asks for the token again, and the sign-in screen says
so rather than leaving the operator to think it broke.

`src/no-browser-storage.test.ts` fails the build if any source file reaches for
browser storage, puts a token in a URL, or reads the token anywhere but the
fetch client. It is a source scan rather than a behavioural test because the
failure it prevents is a "remember me" checkbox added in six months, which no
test of today's code would notice.

### A tool that changes state never looks like one that does not

`effectOf()` in `src/domain/effect.ts` has one rule worth reading: a tool whose
effect the server did not declare counts as **state-changing**, as does one
whose declared effect is a word nobody here recognises. An undeclared effect is
the likely shape of a hostile or careless server, and a console that rendered
it as harmless would be the thing an operator is looking at while deciding.

`ToolEffectBadge` is the single component that renders that verdict, so the
distinction holds in the server's tool list, in the allowlist and inside the
confirmation — and keeps holding when a fourth place to show a tool is added.
The badge carries the word and a glyph, not just a colour.

### Enabling one requires typing its name

Clicking *Allow* on a read-only tool allowlists it. Clicking it on a
state-changing tool opens a typed confirmation: the operator types the tool's
name, exactly, and nothing else enables the button. There is no "enable
anyway", and it is not a dialog, because a dialog is a thing people click
through.

The console then sends `confirm_tool_name`, and the API refuses a mismatch on
its own. This is the first of two gates saying the same thing, not the only
one — if this screen were bypassed entirely, the API would still refuse.

### Every setting says where its value came from

Database, environment or default, as a badge on every row. Without it, a
setting saved here but overridden by an environment variable is
indistinguishable from one that did not save, and the operator's next move —
edit it again, or go and change the deployment — differs completely between the
two. Saving a row whose source is `env` says in words that the environment
still wins.

Credentials are not settings and do not appear: the platform token, the model
key and the database URL are environment-only, the API refuses to store them,
and nothing in `src/api/types.ts` has a field that could carry one.

A refused value is never quoted back, either. `PUT /api/settings/{key}` with
something that will not parse answers with what the setting accepts -- "expected
a number between 0 and 1" -- and records the same sentence in the audit. The box
that most often holds the wrong thing is one an operator has just pasted a
credential into, and `config_audit` is append-only, so a reply or a record that
repeated the value would be a secret nobody can take back out.

The federation screen follows the same rule with more to lose, because its
refusals write the change record rather than the config store: `config_sql`'s
`RECORD_REFUSAL` has no `before`/`after` columns to fill, while
`APPEND_CONFIG_AUDIT` does, and a tool name that reached `after` would be in
the one table with a trigger refusing DELETE. So `POST /api/federation/allowlist`
names what the console *recognises* and nothing else: a tool that no server
offers, a server that is not configured, or a name in a `DELETE` path answers
with "the name that was submitted" plus the servers and tools that do exist,
and records the attempt without the text. A tool the server itself advertised
is different -- that name was already ours -- and a refusal about one does say
which tool it was, which is what makes "somebody tried to enable this" worth
reading a week later.

## Tests

```bash
cd console && npm test
```

- `domain/effect.test.ts` — the effect rule, including that an undeclared or
  `undetermined` effect is state-changing.
- `domain/settings.test.ts` — both spellings of every provenance, and that an
  unrecognised one reads as unknown rather than as a guess.
- `domain/person.test.ts` — reading `platform:id` back apart, and refusing
  rather than guessing.
- `api/client.test.ts` — the request carries the credential and nothing that
  could name an operator; path segments from the API are escaped.
- `no-browser-storage.test.ts` — a source scan; see above.
- `components/format.test.ts` — the status screen's tolerance: which fields
  become cards, and that a null reads as a dash rather than an empty card.
- `components/TypedConfirm.test.tsx` — the enable button, in a DOM.
- `console.e2e.test.tsx` — the whole app against a stub API it starts itself
  (`src/test/fixtures.ts`), covering sign-in, a refused credential, the badge
  distinction, allowlisting read-only, and the typed gate end to end.

The end-to-end test is here because of the bug it caught. Sign-in used to put
the token in the session and verify it afterwards, so a refused credential
unmounted the form mid-request and the operator was left with an empty box and
no message — a wrong token looked exactly like a button that does nothing.
Every piece was individually correct and every unit test passed.

## Known gaps

- **The image does not build this bundle yet.** `ADMIN_CONSOLE_DIR` is set in
  compose and nothing satisfies it; see "In the image" above for the two
  stanzas that fix it. Until then the console runs from a local build, not
  from a deploy.
- **The retention screen has nothing to edit.** The API exposes no retention
  setting — `SETTINGS` in `app/configuration.py` has no retention key, so
  `GET /api/settings` never returns one. The screen says so in those words
  rather than showing a control that would not save; the opt-out half of it
  works. Whoever adds a retention setting to that registry gets the editor for
  free, because the screen filters the settings it is given.
- **The status screen renders whatever the API reports.** If a future status
  field is a credential or a line of message content, this console would draw
  it. That is the API's invariant to hold — `admin/handlers/queries.py` is the
  only module that touches the corpus and it selects no content column — and
  this console does not re-check it.

## Dependencies, and why each one is there

Runtime: **react**, **react-dom**, **react-router-dom**. A framework and a
router, which is the budget.

Build and test: **vite**, **@vitejs/plugin-react**, **typescript**,
**vitest**, **jsdom**.

Nothing else. In particular:

- **No UI library.** Seven screens of tables and forms; a component library
  would be a larger dependency than the application, and the one visual rule
  that matters here — a mutating tool looks different, everywhere — is easier
  to hold in one stylesheet than to enforce across someone else's components.
- **No data-fetching library.** `fetch` plus a 40-line `useResource` hook. The
  console makes one request per screen and reloads after a change.
- **No state library.** React's `useSyncExternalStore` over the session module.
- **No testing library.** The one test that needs a DOM renders the component
  with React's own `createRoot` and `act` into jsdom, and reads
  `button.disabled`. That is a dozen lines against a library that would be a
  larger dependency than the thing under test.

Two dependencies beyond the framework and the router need a justification, and
it is the same one. **vitest** and **jsdom** exist for the places where a quiet
regression is a security hole rather than a cosmetic one: the rule that an
undeclared effect is state-changing, the typed confirmation, the token never
reaching browser storage, and the request never naming an operator. Each of
those is pinned by a test that has been checked to fail when the rule is
removed:

| Remove | Test that goes red |
| --- | --- |
| `disabled={!matches \|\| busy}` on the enable button | `components/TypedConfirm.test.tsx` — 2 tests |
| the read-only spelling list, or the undeclared-is-mutating default | `domain/effect.test.ts` |
| the ban on browser storage | `no-browser-storage.test.ts` |
| the absence of a token-minting call | `api/client.test.ts` — 2 tests |

## The console cannot mint a credential for the corpus

An MCP credential (`cfm_…`) is one *person's* read of the corpus: `mcp/auth.py`
exchanges it for a `PersonRef`, the ACL resolver turns that into a `Viewer`,
and the retrieval tools then return every channel that Discord account can see.
Minting one is therefore not a configuration change but a grant of access to
message content.

So the console does not do it. `POST /api/tokens` does not exist — the request
falls through to the "this console returns no message, document or ask content"
refusal — and the console is not even handed a port that could: `AdminServices`
takes a `TokenDirectory` (review and revoke) rather than a `TokenStore`, so a
route that reached for `issue` fails `mypy` rather than shipping. Recording the
act as an escalation in `config_audit` would have documented it; it would not
have bounded it.

Issuing stays where it takes the agent's own environment and a shell on the
host:

```bash
python -m chatmemory.mcp.issue_token issue <discord_user_id> --label laptop
```

What remains in the console is the half that can only narrow: the listing
(label, person, timestamps — no credential, no hash) and revocation, which is
the operation someone actually needs at 3am.

`tests/unit/test_admin_api.py::test_the_console_cannot_mint_a_corpus_credential`
and `::test_the_console_is_handed_no_way_to_mint_one` are what keep it that way.

## Requests, and what they may not say

Every request carries `Authorization: Bearer <token>` and nothing else that
could be read as an identity. No operator field, no impersonation header, no
name in a query string: the credential decides who is acting, and a request
that could name an operator would make the audit trail an assertion rather than
a fact. `src/api/client.test.ts` asserts that over the request the client
actually builds.

A 401 signs the operator out and says one sentence, the same one for all four
causes. The API deliberately makes missing, malformed, unknown and revoked
indistinguishable; a client that guessed which one it was and said so would
rebuild the oracle the server refuses to be.
