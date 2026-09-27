# The operator console

A Svelte 5 + TypeScript single-page app that talks to the admin API and to
nothing else.
It is static files: no server of its own, no credentials of its own, no build
step at runtime. The admin API serves it from the same origin it serves `/api`
from, which is what lets the bundle use a relative API root, rely on the
CyberdyneAuth session cookie without any CORS, and hold a break-glass token in
memory.

Source is in `console/`. Build output is `console/dist/`, which is
`.gitignore`d — it is built during the image build, not committed.

## Build and run

```bash
cd console
npm ci               # once
npm run lint         # eslint, including sonarjs cognitive complexity <= 12 per function
npm test             # domain, view-models, components, guards, and every screen end to end
npm run build        # svelte-check, then emits console/dist/
```

`just console-check` runs all three, as CI does.

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

### In the image

The `Dockerfile` builds the bundle in a `node:22-slim` stage (`npm ci`, then
`npm run build`, which runs `svelte-check` first so a type error fails the
image) and copies `console/dist` to `/app/console`, which is where
`docker-compose.yml` points `ADMIN_CONSOLE_DIR`. Node is needed at build time
only; nothing in the running image depends on it. `.dockerignore` keeps the
host's `console/node_modules` and `console/dist` out of the build context.

The image's import smoke check includes `chatmemory.entrypoints.admin`, so an
admin service that cannot import fails the build rather than the deployment.

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

### No credential the page can read is ever stored

With CyberdyneAuth sign-in the credential is the `__Host-cf_admin` cookie,
which is `HttpOnly`: JavaScript cannot read it, and the console only ever
learns *who* it names (`GET /api/session`). A break-glass operator token lives
in a module variable in `src/services/session.ts`. Not `localStorage`, not
`sessionStorage`, not a cookie. This console can add a federated server and
enable a tool that changes state somewhere else, so its credential is a grant
of new capability to the agent — and a credential in browser storage outlives
the tab, the session and the attention of the person who pasted it.

The cost is that a reload asks for the token again, and the sign-in screen says
so rather than leaving the operator to think it broke. (A CyberdyneAuth
session survives a reload: the cookie is the browser's, not the page's.)

`src/no-browser-storage.test.ts` fails the build if any source file reaches for
browser storage, puts a token in a URL, reads the token anywhere but the fetch
client (`src/services/http.ts`), writes an `Authorization`/`Bearer` header
anywhere but `src/services/breakGlassHttp.ts`, or asks for cookies
(`credentials: "same-origin"`) anywhere but `src/services/http.ts`. It is a
source scan rather than a behavioural test because the failure it prevents is a
"remember me" checkbox added in six months, which no test of today's code
would notice.

The scan covers every file type the console is written in (`.ts`, `.svelte`,
`.svelte.ts`) and ignores comments, HTML comments included, so a
file can explain why it avoids `localStorage` without failing. It checks
itself against the fixtures in `console/fixtures/storage-guard/`: a Svelte
component and a `.svelte.ts` module that reach for storage must be caught, and
a component that only mentions it in comments must not.

CI runs the console's lint, tests and build in a `console` job of its own, so
a broken console test, a tripped guard or a function over the complexity limit
fails the pull request.

### A tool that changes state never looks like one that does not

`effectOf()` in `src/domain/effect.ts` has one rule worth reading: a tool whose
effect the server did not declare counts as **state-changing**, as does one
whose declared effect is a word nobody here recognises. An undeclared effect is
the likely shape of a hostile or careless server, and a console that rendered
it as harmless would be the thing an operator is looking at while deciding.

`views/components/ToolEffectBadge.svelte` is the single component that renders that verdict, so the
distinction holds in the server's tool list, in the allowlist and inside the
confirmation — and keeps holding when a fourth place to show a tool is added.
The badge carries the word and a glyph, not just a colour.

### Removing something takes a second click

Removing a channel, a federated server, an allowlisted tool, an opt-out or a
token arms the button first; the armed button names the consequence ("Stop
indexing it", "Revoke this token") and only a second click sends the request.
"Keep" disarms it. Not `window.confirm`, which is dismissed by muscle memory
and says nothing about what is being removed.

### Enabling one requires typing its name

Clicking *Allow* on a read-only tool allowlists it. Clicking it on a
state-changing tool opens a typed confirmation: the operator types the tool's
name, exactly, and nothing else enables the button. There is no "enable
anyway", and it is not a dialog, because a dialog is a thing people click
through.

The gate is `TypedConfirmVM` in `src/viewmodels/federation.svelte.ts`;
`FederationVM.confirm` re-checks it before sending, so a button that somehow
got enabled still sends nothing. The console then sends `confirm_tool_name`,
and the API refuses a mismatch on
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
and nothing in `src/domain/types.ts` has a field that could carry one.

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

## How the source is laid out

Svelte 5 (runes) + Vite, without SvelteKit: the server serves static files and
nothing else, and the sign-in flow lives on the Python side, so SSR, load
functions and file-system routing would duplicate or fight it. The source is
in four layers, and `src/architecture.test.ts` fails on an import that breaks
the direction:

| Layer | Holds | May import |
| --- | --- | --- |
| `domain/` | Pure rules and API types (`effect`, `settings`, `person`, `allowlist`, `format`, `roles`, `changed`) | nothing |
| `services/` | `http.ts` (the only `fetch`, cookie mode, the CSRF header, the 401 rule), `breakGlassHttp.ts` (the bearer request for a typed token), `adminApi.ts`, `sessionApi.ts` (who is signed in, sign-in offered, logout), `session.ts`, `hashLocation.ts` | domain, types only |
| `viewmodels/` | `*.svelte.ts` classes: `Resource`, `Action`, `Confirmation`, `SessionVM`, `Router`, one VM per screen (`StatusVM`, `FederationVM` with `ServersVM`/`AllowlistVM`/`TypedConfirmVM`, `ChannelsVM`, `RetentionVM`, `SettingsVM`/`SettingEditor`, `TokensVM`, `AuditVM`), `ConsoleVM` | services, domain |
| `views/` | `App`, `Shell`, `SignIn`, `screens/*Screen.svelte`, `components/` (badges, `ConfirmButton`, `TypedConfirm`, `ServerCard`, `SettingsTable`, `Loaded`, `Panel`, `ActionResult`) | viewmodels, domain |

`main.ts` is the composition root: it builds a `ConsoleVM` from the real
services and mounts `views/App.svelte`. A screen asks the `ConsoleVM` for its
view-model (`app.channels()`), loads it on mount and renders only what it
holds, so no view names a service, and a test builds a view-model with fakes
(`new ChannelsVM(fakeApi)`) and drives it without a DOM.

The router is a hash router over a route table in `views/routes.ts`. Every
route declares a `minRole` (`operator` or `admin`) with no default; the
navigation shows the routes the session's role allows, and the server stays
the authority. An address that names no screen, or a screen above the
signed-in role, is rewritten to `#/status`.

## Tests

```bash
cd console && npm test
```

- `domain/*.test.ts` — the effect rule (an undeclared or `undetermined`
  effect is state-changing), both spellings of every provenance, reading
  `platform:id` back apart and refusing rather than guessing, the status
  screen's tolerance (which fields become cards, a null reads as a dash), and
  allowlist keys.
- `services/*.test.ts` — cookie mode sends no `Authorization` and
  `credentials: "same-origin"`, with the CSRF header on writes only; token
  mode sends the bearer with `credentials: "omit"`; nothing could name an
  operator; path segments from the API are escaped; `/api` and `/auth` are
  resolved beside the page, so a prefixed mount works; a 401 signs out with one
  sentence; logout posts with the CSRF header and returns the end-session URL;
  there is no call that mints an MCP credential.
- `viewmodels/*.test.ts` — every screen's behaviour without a DOM: the typed
  gate and the read-only fast path, the note a save gives when the environment
  still wins, the unreadable-channel note, opt-outs that cannot be parsed,
  tokens without an id, the audit filter, the two-click `Confirmation`.
- `views/components/TypedConfirm.test.ts` and `ConfirmButton.test.ts` — the
  enable button and the two-click removal, in a DOM.
- `views/Shell.test.ts` — the navigation leaves out a route the role may not
  open, and says who is signed in with which role.
- `views/routes.test.ts` — every screen's `minRole` equals the access the
  server's `ROUTE_ACCESS` asks for the reads that screen makes (it reads
  `src/chatmemory/admin/server.py`, so a role change on either side fails it).
- `architecture.test.ts` and `no-browser-storage.test.ts` — source scans; see
  above. Each checks itself against the fixtures in `console/fixtures/`.
- `console.e2e.test.ts` — the whole app against a stub API it starts itself
  (`src/test/stubApi.ts`): sign-in refused and accepted, sign-out, an unknown
  address, the badge distinction, allowlisting read-only, the typed gate end to
  end, adding a server, two-click removal of one server (and no removal while
  another change is in flight), revoking an allowlisted tool, channel
  readability and adding an unreadable channel, settings provenance and a save
  the environment overrides, the retention message, saving a retention setting
  under its own key, adding an opt-out, two-click removals of a channel, an
  opt-out and a token, and the audit's refusals and filter. With CyberdyneAuth
  offered: the sign-in link first and the token form behind "Use an operator
  token"; a cookie session opening the console with no credential header; an
  operator seeing every screen with no control that changes anything; an admin
  write carrying the CSRF header (the stub refuses it otherwise); sign-out
  ending the server session before leaving for the provider; and a
  break-glass token sent without the stale cookie the browser still holds.

The end-to-end test is here because of the bug it caught. Sign-in used to put
the token in the session and verify it afterwards, so a refused credential
unmounted the form mid-request and the operator was left with an empty box and
no message — a wrong token looked exactly like a button that does nothing.
Every piece was individually correct and every unit test passed.

## Known gaps

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

Runtime: none. Svelte compiles to plain JavaScript; the bundle ships no
framework runtime beyond what the compiler emits, and no router (the hash
router is about thirty lines in `viewmodels/router.svelte.ts`).

Build and test: **svelte**, **vite**, **@sveltejs/vite-plugin-svelte**,
**typescript**, **svelte-check**, **vitest**, **jsdom**,
**@testing-library/svelte**, and **eslint** with **typescript-eslint**,
**eslint-plugin-svelte** and **eslint-plugin-sonarjs** (for the
cognitive-complexity limit).

Nothing else. In particular:

- **No UI library.** Seven screens of tables and forms; a component library
  would be a larger dependency than the application, and the one visual rule
  that matters here — a mutating tool looks different, everywhere — is easier
  to hold in one stylesheet than to enforce across someone else's components.
- **No data-fetching library.** `fetch` in one file plus the `Resource`
  view-model. The console makes one request per list and reloads after a
  change.
- **No state library.** Runes (`$state`, `$derived`) in the view-model classes.

The test dependencies exist for the places where a quiet regression is a
security hole rather than a cosmetic one: the rule that an undeclared effect
is state-changing, the typed confirmation, the token never reaching browser
storage, and the request never naming an operator. Each of those is pinned by
a test that fails when the rule is removed:

| Remove | Test that goes red |
| --- | --- |
| `disabled={!gate.canConfirm(busy)}` on the enable button | `views/components/TypedConfirm.test.ts` — 2 tests |
| the name check in `FederationVM.confirm` | `viewmodels/federation.test.ts` |
| the read-only spelling list, or the undeclared-is-mutating default | `domain/effect.test.ts` |
| the ban on browser storage | `no-browser-storage.test.ts` |
| the absence of a token-minting call | `services/adminApi.test.ts` — 2 tests |

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

A request carries one credential: the session cookie (the browser attaches it
because `http.ts` asks for `credentials: "same-origin"`; writes add
`X-CyberFriend-Console: 1`), or, after a break-glass sign-in,
`Authorization: Bearer <token>` with `credentials: "omit"` from
`breakGlassHttp.ts`. Never both, and nothing else that could be read as an
identity. No operator field, no impersonation header, no
name in a query string: the credential decides who is acting, and a request
that could name an operator would make the audit trail an assertion rather than
a fact. `src/services/adminApi.test.ts` asserts that over the request the
client actually builds.

A 401 signs the operator out and says one sentence, the same one for all four
causes. The API deliberately makes missing, malformed, unknown and revoked
indistinguishable; a client that guessed which one it was and said so would
rebuild the oracle the server refuses to be.

## Roles, and which route needs which

The API has two console roles. **Operator** is read-only: status, settings,
federation, channels, opt-outs, tokens and the audit, and nothing that changes
them. **Admin** implies operator and is required for every write — a setting,
a federated server or tool, a channel, an opt-out (which purges), an MCP token
revocation.

Which role a route needs is decided in one place, `ROUTE_ACCESS` in
`src/chatmemory/admin/server.py`, with one explicit row per mounted
`(method, path)`: `public` (probes and the static bundle), `user` (the user
area, later), `operator`, `admin`, or `admin_oidc` (personal content: admin
and signed in as a person, never a token). There is no default by method; HEAD
is looked up under its route's GET row. The middleware resolves each request
against the router's own route list, so:

- a route with no row is refused with 403 `{"error":"forbidden"}` before its
  handler runs;
- a principal below the route's role gets 403 `{"error":"requires admin"}`;
- an unauthenticated request still gets the one byte-identical 401, whatever
  the route's role.

"No row" covers the open routes' other verbs too. `/health`, `/ready` and the
bundle are public for GET (and HEAD) only: `POST /health` or `POST
/index.html` is no longer the router's 405 but a 401 without a credential and
a 403 `{"error":"forbidden"}` with one. Everything whose path starts with
`/api` is authenticated first even when the bundle's mount at `/` is what
claims it, so `GET /api` or `GET /apix` without a credential is the same 401
as any API route.

`tests/unit/test_admin_roles.py` walks every mounted route and fails on a
route without a row, a row without a route, a write below admin, or anything
under `/api` marked public. Adding a route means adding its row in the same
diff.

### What a `cfa_` token is

While CyberdyneAuth sign-in is not configured (`ADMIN_OIDC_ISSUER` unset), a
`cfa_` token is admin, exactly as before roles existed. Once sign-in is
configured, every `cfa_` token is **operator only**: admin rights then come
only from the CyberdyneAuth role. A token never reads personal content
(`admin_oidc` routes), whatever its role. Unsetting the issuer is the
break-glass rollback: tokens are admin again on the next start, and sessions
stop being accepted. The other four sign-in variables may stay set; without an
issuer they are ignored, with a warning naming them.

## Signing in with CyberdyneAuth

The admin API is a backend-for-frontend: it runs the OIDC authorization code
flow with PKCE (S256) against CyberdyneAuth itself, and the browser only ever
holds an opaque cookie. No access or refresh token ever reaches the browser,
and the id token only once: as the `id_token_hint` of the end-session URL at
sign-out, after the session it belonged to has been revoked (RP-initiated
logout needs it, and it is not a credential for this API).
The code is in `src/chatmemory/admin/oidc/`.

| Route | Access | What it does |
|---|---|---|
| `GET /auth/config` | public | `{"sign_in": true}` or `false`: whether the console offers "Sign in with CyberdyneAuth". Answered with sign-in off too. |
| `GET /auth/login` | public | Stores a login record (10 minutes), sets `__Host-cf_login` (`HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age=600`) and redirects to CyberdyneAuth. |
| `GET /auth/callback` | public | Consumes the login once, requires the `__Host-cf_login` value, exchanges the code, verifies both tokens and userinfo, sets `__Host-cf_admin` (`HttpOnly; Secure; SameSite=Strict; Path=/`) and redirects to `/#/status`. |
| `POST /auth/logout` | public, CSRF header | Revokes the session, then the refresh token at CyberdyneAuth (best effort), clears the cookie and returns `{"end_session_url": ...}` with `id_token_hint` and `post_logout_redirect_uri`. The console navigates there. |
| `GET /api/session` | operator | `{"subject", "display", "roles", "via"}` for whoever the credential names. |

With sign-in off, `/auth/login`, `/auth/callback` and `/auth/logout` answer
404.

**Verification**, exactly per the CyberdyneAuth contract. Access token: RS256
via the issuer's JWKS, `iss` equal to discovery's, `exp`, `type == "access"`,
`aud == "cyberfriend"`. Id token: RS256, `iss`, `aud == client_id`, `exp`, and
the nonce issued for that sign-in. `userinfo.sub == id_token.sub ==
access_token.sub` is required before the email is read. People are keyed on
`sub`. The key set is refetched when an unknown `kid` arrives, at most once a
minute.

**Roles** come from the verified access token only: `<client_id>:admin` is
admin (and operator), `<client_id>:operator` is operator. A roles claim that
is absent, or has neither role, gives no session; the browser sees a fixed
"No access" page that does not say whether the account exists.

**Sessions** live in `admin_session` (migration 0031): the id as its sha256,
the tokens AES-GCM under `ADMIN_SESSION_KEY`. Every request re-verifies the
stored access token. Within 60 seconds of expiry it is refreshed first, under
a per-session lock (refresh tokens rotate with reuse detection, so two
refreshes in flight would revoke the family; the lock holds while the admin
service runs one replica). Roles are re-checked on every refresh:

- no roles claim: the session is revoked and the request gets the standard 401;
- neither console role: the session is revoked and the request gets 403
  `{"error":"no console access"}`;
- admin lowered to operator: the session continues as operator.

A role withdrawn at CyberdyneAuth therefore takes effect within 15 minutes. A
session also ends when its refresh token expires (30 days), on sign-out, when a
refresh fails, or after 12 hours without a request.

**A bearer header wins.** A request with `Authorization` is authenticated by it
alone, and its `Cookie` header is removed before any handler sees it: the
session is not read, refreshed or used as a fallback, and an invalid bearer is
a 401 even with a valid cookie.

**CSRF.** Every cookie-authenticated request that is not a read, and every
`POST /auth/logout`, must carry `X-CyberFriend-Console: 1`; without it the
answer is 403 `{"error":"cross-site request refused"}`. Any non-read that sends
an `Origin` must send the console's own (`ADMIN_PUBLIC_URL`'s origin). Every
response carries `Content-Security-Policy: default-src 'self'; frame-ancestors
'none'; base-uri 'none'; form-action 'self' <issuer>`,
`X-Content-Type-Options: nosniff` and `Referrer-Policy: no-referrer`.

**Audit.** A change made through a session is recorded with actor
`oidc:<sub>` and the person's email in `config_audit.operator_display`
(`/api/audit` returns it as `operator_display`). Token changes are recorded
exactly as before. The record stays append-only.

### Configuration

The issuer is the switch. Unset (or blank), sign-in is off whatever else is
set, which is what makes unsetting it alone a working rollback. Set, the other
four are required: a set issuer with any of them missing refuses to start and
names what is missing (the issuer alone would downscope every token with no way
left to sign in as admin).

| Variable | Value |
|---|---|
| `ADMIN_OIDC_ISSUER` | `https://auth.backend.coolify.cyberdynecorp.ai` |
| `ADMIN_OIDC_CLIENT_ID` | the registered BFF client, `cyberfriend` |
| `ADMIN_OIDC_CLIENT_SECRET` | its secret (never logged or shown) |
| `ADMIN_SESSION_KEY` | 32 random bytes, base64: `openssl rand -base64 32` (never logged or shown) |
| `ADMIN_PUBLIC_URL` | the console's https URL, e.g. `https://admin.example.com` |
| `ADMIN_OIDC_SCOPES` | optional; default `openid email profile` |

Register the client at CyberdyneAuth as a confidential client with redirect
URI `<ADMIN_PUBLIC_URL>/auth/callback` and post-logout redirect URI
`<ADMIN_PUBLIC_URL>/`, and assign people `cyberfriend:admin` or
`cyberfriend:operator`.

### Enabling sign-in on Coolify

Set the variables in Coolify on the **admin** service's environment.
`docker-compose.yml` declares them for `admin` alone (Coolify accepts only the
variables the compose file declares), so no other service receives the client
secret or the session key.

1. Register the client at CyberdyneAuth as above, with redirect URI
   `<ADMIN_PUBLIC_URL>/auth/callback` and post-logout redirect URI
   `<ADMIN_PUBLIC_URL>/`.
2. On the `admin` service, set `ADMIN_OIDC_ISSUER`, `ADMIN_OIDC_CLIENT_ID`,
   `ADMIN_OIDC_CLIENT_SECRET`, `ADMIN_SESSION_KEY` and `ADMIN_PUBLIC_URL`
   (and `ADMIN_OIDC_SCOPES` only to change the default).
3. Set `ADMIN_PUBLIC_URL` to exactly the https origin Coolify serves for
   `SERVICE_FQDN_ADMIN_8083`: `https://`, the host, no path, no trailing slash.
   The console compares each browser write's `Origin` header to it by exact
   string match, so any difference (`http`, another host, a port, a path)
   refuses every cookie-mode write and `POST /auth/logout` with 403, and the
   callback URI sent to CyberdyneAuth no longer matches the registered one.
4. Redeploy `admin`. A partial set refuses to start and the log names the
   missing variable.
5. Check: `curl https://<admin host>/auth/config` returns `{"sign_in": true}`,
   and the console's first page offers "Sign in with CyberdyneAuth".

To switch sign-in off again, unset `ADMIN_OIDC_ISSUER` on `admin` and redeploy.

### In the console

On load the console asks `GET /auth/config` whether sign-in is offered and
`GET /api/session` whether the cookie already names somebody (a person coming
back from the callback holds nothing else), and shows nothing until both
answer.

- **Sign-in offered, no session:** a "Sign in with CyberdyneAuth" link to
  `auth/login` (a full-page navigation; the code and tokens never pass through
  the bundle), and a "Use an operator token" button that reveals the token
  form.
- **Sign-in not offered:** the token form alone, as before sign-in existed.
- **Signed in:** the navigation names the person and their role. An operator
  sees every screen and no control that changes anything — no add form, no
  Allow, Edit, Remove, Revoke or Opt back in. That is a courtesy: the API
  refuses an operator's write with 403 whatever the page shows.
- **Sign out:** with a CyberdyneAuth session, `POST /auth/logout` revokes it
  first, then the browser goes to CyberdyneAuth's end-session URL. With a
  token, the token is forgotten; there is no server session to end.

**Break-glass.** A token typed into the form is checked with
`GET /api/session` as a bearer before it is held, and the API's answer decides
its role: admin while the issuer is unset, operator once it is set. So with
CyberdyneAuth down and an admin change urgent, the way in is the rollback in
[What a `cfa_` token is](#what-a-cfa_-token-is): unset `ADMIN_OIDC_ISSUER`,
redeploy `admin`, and sign in with a token; the page then offers the token
form alone. Set the issuer again when CyberdyneAuth is back.
