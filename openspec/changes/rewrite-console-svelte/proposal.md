## Why

The console (`console/`, React 18 + react-router 6) is about to grow: a login
flow, role-dependent screens, a usage view and a feature-request triage view.
The team has chosen Svelte with an MVVM structure for its frontends, and today's
screens mix fetching, state and markup in one component (FederationScreen is 247
lines). Porting now, before the new screens exist, costs seven screens instead
of ten and gives the new work a layout where state lives in testable
view-models rather than in components.

The port also closes a gap found while planning it: CI never runs the console's
tests, its no-browser-storage guard or a lint (`.github/workflows/ci.yml`
46-94). The only console gate is the Docker build, and it does not run the
tests.

## What Changes

- Replace the React tree in `console/` with Svelte 5 (runes) + Vite, not
  SvelteKit. Keep `base: "./"`, `outDir: dist`, sourcemaps, the dev proxy and
  the hash router, so the Dockerfile stage and `ADMIN_CONSOLE_DIR` are unchanged.
- Layers: `domain/` (pure TypeScript), `services/` (the only code that calls
  `fetch`), `viewmodels/` (`*.svelte.ts` classes with `$state`/`$derived`),
  `views/` (screens and components that bind to a view-model). An
  architecture test enforces the import direction.
- Port the seven screens one for one (Status, Federation, Channels, Retention,
  Settings, Tokens, Audit), plus SignIn, with the same API calls and the same
  safety gestures (two-click confirm, typed confirm for mutating tools).
- Move the pure domain modules (`effect`, `settings`, `person`, `format`) and
  their tests over unchanged.
- Extend the no-browser-storage guard to `.svelte` and `.svelte.ts` sources.
- Add a `console` CI job: `npm ci`, lint (including eslint-plugin-sonarjs
  cognitive complexity), tests, build.
- Rewrite `docs/admin-console.md` for the new bundle and correct its stale
  statements about the Docker build.

Non-goals:

- No change to the admin API, to authentication (still the bearer token entered
  in SignIn; the CyberdyneAuth login is `add-console-cyberdyneauth-login`), or
  to what any screen shows.
- No new screens.

## Capabilities

### New Capabilities

- `admin-console-ui`: how the console bundle is structured, tested, built and
  served.

### Modified Capabilities

None. `admin-console` (the API contract) is untouched.

## Impact

- `console/` is replaced wholesale. Dev dependencies change from React,
  react-router and plugin-react to svelte, @sveltejs/vite-plugin-svelte,
  svelte-check, @testing-library/svelte, eslint, eslint-plugin-svelte and
  eslint-plugin-sonarjs.
- `.github/workflows/ci.yml` gets a console job.
- `Dockerfile` keeps the same `console` stage and copy path.
- `docs/admin-console.md` is rewritten.
