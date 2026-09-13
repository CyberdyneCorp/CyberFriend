## Why

The corpus built in `add-discord-chat-memory` answers single-shot lookups well, but the questions people actually ask are multi-step. *"What do I need to do today?"* means: find what was asked of me, work out which of those are still open, check whether I already replied, and rank what matters. One retrieval pass cannot do that — it needs a plan, several retrievals, and a judgement about whether the evidence gathered is sufficient.

Separately, the answers people want frequently live outside Discord. *"Did anyone follow up on the auth bug?"* spans a conversation, a GitHub issue, and a Linear ticket. Rather than building an integration per system, CyberFriend should act as an **MCP client** and federate tools from existing MCP servers — the same servers the team already runs.

These two features are proposed together because they are the same feature: a reasoning loop whose tools happen to include both internal retrieval and external systems.

They are also, combined, the most dangerous change in this project. CyberFriend already holds private channel content; it already ingests content written by anyone in the server; federation would add the ability to act on external systems. That combination means a crafted Discord message could otherwise steer the agent's tool calls — a server member gaining indirect control of the bot's credentials. **Most of this proposal exists to make that impossible**, which is why authorization and content-handling are specified as a capability rather than left as implementation care.

## What Changes

- Add a bounded reasoning loop: plan a query into sub-questions, retrieve iteratively, assess sufficiency, refine, and stop against explicit step/token/time budgets.
- Add self-correction — recognise a weak or empty result set and reformulate rather than answering from nothing.
- Add MCP client capability: connect to configured external MCP servers, discover their tools, namespace them to avoid collisions, and expose them to the loop.
- Add tool routing, so the loop sees a relevant subset of federated tools rather than every tool from every server.
- Add an authorization model in which the agent acts **only on authority derived from the requesting human**, external tools are read-only by default, and mutating tools require explicit per-invocation confirmation.
- Add structural separation of retrieved content from instruction, so indexed messages are always data and never direction.
- Add an audit record for every external tool invocation: who asked, what was retrieved, what was called, with which arguments.

Non-goals for this change:

- Ask/commitment extraction and scheduled digests (separate change) — this change makes *"what do I need to do today?"* answerable by reasoning; extraction later makes it cheap and reliable.
- The Discord bot surface itself (separate change).
- Slack.
- Autonomous or scheduled action without a human request. Every loop in this change begins with a person asking something.
- CyberFriend hosting or proxying MCP servers for other clients.

## Capabilities

### New Capabilities

- `agent-reasoning`: planning a question into steps, iterative retrieval with sufficiency assessment and self-correction, bounded termination, and answers grounded in cited evidence.
- `mcp-federation`: connecting to external MCP servers, discovering and namespacing their tools, routing a relevant subset into the loop, and degrading gracefully when a server is unavailable.
- `agent-authorization`: the contract that the agent's authority comes from the requesting person and never from content it has read — read-only default, confirmation for mutations, untrusted-content fencing, and a non-repudiable audit trail.

### Modified Capabilities

None. `mcp-interface` from the in-flight `add-discord-chat-memory` change covers CyberFriend's *inbound* surface — CyberFriend as an MCP server. This change adds the *outbound* client side as a separate capability, so no existing requirement changes. The two are named distinctly (`mcp-interface` inbound, `mcp-federation` outbound) precisely because conflating them is the easy mistake.

## Impact

- **Depends on** `add-discord-chat-memory` — the reasoning loop's primary tools are that change's retrieval interface, and its permission filter remains the mechanism that keeps the loop inside the viewer's visibility.
- **New dependency:** an MCP client library, plus per-server transport configuration and credentials.
- **New failure modes:** unbounded loops, external servers that hang or fail, and tool-selection degradation as the federated tool count grows.
- **Cost and latency rise materially.** A multi-step loop with tool calls costs several times a single-shot answer; budgets are specified as requirements, not tuning.
- **Security posture changes fundamentally.** Before this change, a defect leaks information; after it, a defect can cause action on external systems. The read-only default and per-invocation confirmation are deliberate friction, and removing them is a decision that needs to be made explicitly and with its consequences understood.
- **Credential design is a prerequisite.** If CyberFriend holds ambient org-wide write credentials, then every server member effectively wields them through the bot. Federated credentials must be either per-viewer or narrowly scoped and read-only.
