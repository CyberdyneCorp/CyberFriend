## ADDED Requirements

### Requirement: Screen parity with the previous console

The console SHALL offer the Status, Federation, Channels, Retention, Settings,
Tokens and Audit screens and the sign-in screen, each making the same API calls
and applying the same confirmation rules as the console it replaces.

#### Scenario: Enabling a mutating federated tool
- WHEN an operator allowlists a tool the server reports as state-changing
- THEN the console SHALL require the exact tool name to be typed before it
  sends the request

#### Scenario: Removing something
- WHEN an operator removes a channel, server, opt-out or token
- THEN the console SHALL require a second, deliberate click before sending the
  request

#### Scenario: Unknown screen
- WHEN the address names a screen that does not exist
- THEN the console SHALL show the Status screen

### Requirement: Views do not fetch

Only the services layer SHALL perform network requests. Views SHALL depend only
on view-models and pure domain code, and view-models SHALL depend only on
services and domain code.

#### Scenario: A view imports a service
- WHEN a view or component imports from the services layer, or calls `fetch`
- THEN the console's test suite SHALL fail

#### Scenario: Testing a screen's behaviour
- WHEN a view-model is constructed with a fake service
- THEN its behaviour SHALL be testable without a DOM or a network

### Requirement: No credential or personal data in browser storage

The console SHALL NOT write to localStorage, sessionStorage, IndexedDB,
`document.cookie` or `window.name`, and SHALL NOT put a credential in a URL.
The check SHALL cover every source file type the console is written in.

#### Scenario: A Svelte component uses localStorage
- WHEN any `.ts`, `.svelte` or `.svelte.ts` source under the console references
  a forbidden storage API
- THEN the console's test suite SHALL fail

### Requirement: Serving is unchanged

The built console SHALL be served by the admin API from the same directory and
under the same URL as before, and SHALL work under any path prefix.

#### Scenario: Deployed behind a path prefix
- WHEN the admin API is mounted under a prefix
- THEN the console SHALL load its assets and reach `/api` relative to that
  prefix

### Requirement: Console checks run in CI

Every change to the repository SHALL run the console's lint, including a
cognitive-complexity limit, its tests and its build.

#### Scenario: A console test fails
- WHEN a pull request breaks a console test or the storage guard
- THEN CI SHALL fail
