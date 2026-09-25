## Why

Everything the agent may reach is currently an environment variable. Adding an
MCP server, indexing another channel, or changing the retention window all mean
editing Coolify and redeploying — so the settings that most need to change
carefully are the ones changed most carelessly, by whoever happens to have
platform access, with no record of who changed what or why.

Federation makes this pressing. The allowlist decides which external tools the
agent can invoke; it will be edited far more often than a deploy, and it is
exactly the configuration where "who turned this on, and when" needs an answer.

## What Changes

- Add an admin console: a web interface for configuring MCP servers and their
  tool allowlist, indexed channels, retention, per-person opt-outs, and MCP
  tokens, with read-only views of health, ingestion progress and the audit log.
- Move editable configuration into the database, so a change applies without a
  redeploy. Secrets stay in the environment and are never readable through the
  console.
- Authenticate admins with bearer tokens that identify a named operator, so
  every configuration change is attributable to a person rather than to
  "whoever held the token".
- Record every configuration change — who, when, what before, what after.
- Require an explicit, deliberate confirmation to enable a state-changing
  federated tool, and record it distinctly from ordinary changes.

Non-goals:

- Reading or editing message content. The console configures the agent; it is
  not a window into the corpus, and making it one would create a second path to
  private channels that bypasses every viewer check.
- Editing secrets. The Discord token, the model key and the database URL stay
  environment-only, and the console can neither display nor set them.
- User-facing settings. This is an operator tool.

## Capabilities

### New Capabilities

- `admin-console`: authenticating an operator, presenting the configuration
  they may change, and recording what they changed.
- `runtime-configuration`: configuration held in the database, its precedence
  against the environment, and how a running process picks up a change.

### Modified Capabilities

None. `mcp-federation` already defines what an allowlist means and what
happens when a server is unreachable; this change alters where that
configuration is stored, not what it does.

## Impact

- **New service.** An HTTP API and a single-page interface, deployed alongside
  the existing three and reachable at its own domain.
- **New tables** for configuration, admin tokens and the configuration audit.
- **Configuration precedence changes.** Editable settings read from the
  database first and fall back to the environment, so an operator can see why a
  value is what it is.
- **This is the highest-privilege surface in the system.** It decides what the
  agent may reach and what gets archived. A compromised admin session can add a
  federated server, enable a mutating tool, or widen the archive — so it is
  authenticated separately from everything else, it holds no Discord or model
  credentials, and every change it makes is attributable.
- **Enabling a mutating tool from a UI is a deliberate reduction in friction.**
  It was previously a code change under review. The per-invocation confirmation
  the requester must give at call time becomes the load-bearing guard rather
  than one of two.
