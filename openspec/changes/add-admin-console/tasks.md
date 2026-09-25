## 1. Schema

- [x] 1.1 Migration `0010`: `app_setting` (key, value, updated_by, updated_at), `admin_token` (hash, operator, label, revoked_at), `config_audit` (operator, setting, before, after, kind, at) (landed as migrations 0011 and 0012)
- [ ] 1.2 Tables for federation config the console edits: `mcp_server`, `mcp_allowed_tool`
- [x] 1.3 Register every new statement in the SQL audit with a written reason
- [x] 1.4 Test: `config_audit` has no update or delete path reachable from the console

## 2. Stored configuration

- [x] 2.1 Implement the resolver: database, then environment, then default — reporting which one a value came from
- [x] 2.2 Refuse to store any secret (platform token, model key, database URL); test each by name
- [x] 2.3 Fail startup, naming the setting, when a required secret is absent from the environment
- [x] 2.4 Implement bounded refresh in each long-running process
- [x] 2.5 **On refresh failure keep the current configuration** — never fall back to the environment, which would silently undo a narrowing an operator just made
- [x] 2.6 Skip and report a malformed stored value rather than stopping the process
- [x] 2.7 Test: removing a channel from scope stops ingestion within the refresh period, with no restart

## 3. Admin authentication

- [x] 3.1 Issue per-operator tokens, stored hashed, with a label and revocation
- [x] 3.2 Resolve the operator from the credential; a request must not be able to name one
- [x] 3.3 Return an identical refusal for missing, malformed, unknown and revoked credentials
- [x] 3.4 Operator CLI to issue and revoke tokens
- [x] 3.5 Test: revoking one operator leaves every other token working

## 4. Audit

- [x] 4.1 Record operator, setting, before, after and time for every change
- [x] 4.2 Record refused changes with the reason
- [x] 4.3 Mark enabling a mutating tool as an escalation, distinct from an ordinary edit
- [x] 4.4 Test: a change made through the API appears in the audit with both values

## 5. Configuration API

- [x] 5.1 Federation: add, edit, remove servers; probe reachability and list offered tools
- [x] 5.2 Federation: manage the allowlist; refuse a tool no configured server offers
- [x] 5.3 Require a confirmation naming the tool to enable a mutating one; refuse a mismatch
- [x] 5.4 Treat an undeclared effect as mutating
- [x] 5.5 Indexed channels: add and remove, reporting a channel the agent cannot read
- [x] 5.6 Retention window and per-person opt-outs
- [x] 5.7 MCP token review and revoke — **no issue route**: minting one grants a read of that account's whole view of the corpus, so it stays in `python -m chatmemory.mcp.issue_token`; the console is typed to a directory that cannot mint
- [x] 5.8 Read-only status: health, ingestion progress, embedding backlog, recent audit
- [x] 5.9 **Refuse any request for message, document or ask content** — counts and timings only
- [x] 5.10 Test: no response anywhere in the API contains a secret, even masked

## 6. Console interface

- [x] 6.1 TypeScript single-page app, built with Vite, served as static files by the API
- [x] 6.2 Federation screen: servers, discovered tools, allowlist, with mutating tools visually distinct
- [x] 6.3 Typed confirmation dialog for enabling a mutating tool
- [x] 6.4 Channels, retention and opt-out screens
- [x] 6.5 Status screen and audit log view
- [x] 6.6 Show the source of each value — database, environment or default
- [x] 6.7 Token entry held in memory only, never in local storage
- [x] 6.8 Tokens screen reviews and revokes; it offers no way to issue one

## 7. Deployment

- [x] 7.1 Add the `admin` service to the compose file, database credentials only
- [x] 7.2 Give it its own domain; no Discord token, no model key, no platform credentials
- [x] 7.3 Add `ADMIN_*` settings to the compose environment so the platform accepts them
- [x] 7.4 Document setup and first-token issuance in the runbook
- [x] 7.5 Test: the admin service cannot reach Discord or the model endpoint

## 8. Verification

- [ ] 8.1 Add a federated server through the console, confirm the agent uses it without a redeploy
- [ ] 8.2 Remove a channel from scope, confirm ingestion stops within the refresh period
- [ ] 8.3 Enable a mutating tool, confirm the escalation is recorded and the per-invocation confirmation still gates the call
- [ ] 8.4 Confirm a revoked admin token stops working and others do not
- [ ] 8.5 Attempt to read corpus content through every endpoint; confirm refusal
