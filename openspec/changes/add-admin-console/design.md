## Context

Every setting lives in Coolify today, so changing one means a redeploy and
leaves no record. Federation is what forces the issue: the allowlist decides
which external tools the agent may invoke, it will change far more often than a
deploy, and it is exactly the configuration where "who turned this on" needs an
answer.

Three decisions were taken deliberately, two of them trading safety for
convenience. They are recorded here with what compensates for them, because a
later reader will otherwise assume they were oversights.

## Goals / Non-Goals

**Goals:**

- Change what the agent may reach and index without a redeploy.
- Attribute every change to a person.
- Never expose the corpus or the agent's credentials through this surface.

**Non-Goals:**

- Reading message content. The console configures; it does not query.
- Editing secrets.
- End-user settings. This is an operator tool.

## Decisions

### Static bearer tokens, but each one names an operator

Discord OAuth with a role check was the safer option and was not chosen; static
tokens were, for the same reason the MCP endpoint uses them — no flow to build.

The failure mode of a shared token is that the audit log can only ever say "the
token did it", which would make the change record decorative precisely where it
matters most. So tokens are issued **per operator** and stored hashed: the
credential identifies who is acting, not merely that someone may. That keeps
the simplicity of bearer auth and recovers attribution, and it means revoking
one operator does not lock everyone out.

A request cannot name the operator it acts as. Only the credential decides —
the same rule the MCP interface follows, and for the same reason: a
caller-supplied identity is an assertion.

### Mutating tools can be enabled from the console

Previously this required editing configuration under review. Doing it from a UI
is a real reduction in friction, and the honest consequence is that the
**per-invocation confirmation becomes the only remaining guard** rather than
the second of two. That gate already exists and is tested; it now carries more
weight than it was designed to.

Two things make the reduction deliberate rather than accidental: enabling such
a tool requires a confirmation naming that specific tool, so it cannot be done
by clicking through, and it is recorded as an escalation of what the agent may
do rather than as an ordinary edit. A tool whose effect is undeclared counts as
mutating, which is the existing rule and is not relaxed here.

### Configuration in the database, secrets in the environment

Editable settings move to the database; the Discord token, the model key and
the database URL stay environment-only and are neither readable nor writable
through the console.

The split is not about sensitivity alone. Those three are needed *to reach* the
database, so storing them there is circular — and a console that can read the
bot token can impersonate the bot everywhere it is installed, which is a much
larger authority than configuring it.

Precedence is database, then environment, then default, and an operator is
shown which one a value came from. Without that, a setting edited in the
console but overridden somewhere else is indistinguishable from one that did
not save.

### A failed refresh keeps the current configuration

When stored configuration cannot be read, a process keeps what it has rather
than falling back to the environment. Falling back reads as the safe choice and
is the opposite: an operator who has just *removed* a channel from indexing
scope, or *revoked* a tool, would have that narrowing silently undone by a
database blip. Configuration changes are asymmetric — the dangerous direction
is widening — so the safe default is to change nothing.

### The console holds no credentials of its own

It reaches the database and nothing else. No Discord token, no model key, no
Coolify API access. A console that could redeploy would need platform
credentials, which is a far larger privilege than the job requires, and it is
why stored configuration exists rather than the console writing environment
variables.

## Risks / Trade-offs

- **This is the highest-privilege surface in the system.** It decides what the
  agent may reach and what is archived. A compromised admin token can add a
  federated server, enable a mutating tool, or widen the corpus. Tokens are
  per-operator and hashed, every change is recorded, and the console holds no
  other credentials — but none of that prevents a stolen token being used.
- **Attribution depends on tokens not being shared.** Nothing technical stops
  two people using one token, and the record would then name the wrong person.
  Issuing one per operator is what makes the log meaningful.
- **Config that changes without a deploy changes without review.** That is the
  point, and it is also the cost: there is no pull request on a checkbox. The
  audit record is the only remaining review, which is why it captures before
  and after rather than just the fact of a change.
- **Validation at submission can go stale.** A server reachable when
  allowlisted may be gone by the time the agent calls it. Federation already
  degrades gracefully when a server is unavailable, so this makes bad
  configuration obvious early rather than guaranteeing it stays good.
- **Refresh introduces a window** between a change and a process applying it.
  Bounded and configurable, but a revoked tool remains callable for that
  period; anything needing immediate effect still wants a restart.
