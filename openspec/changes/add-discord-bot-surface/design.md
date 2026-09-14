## Context

This is the first surface anyone actually touches. Everything prior is infrastructure reachable only over MCP.

One decision dominates the design. Answers are **public in the channel by default**, which was chosen deliberately: a bot that only ever whispers never becomes part of how the team works, and the answers are usually useful to more than one person. But the earlier changes specified answers as reaching only the requester, and for good reason — an answer can draw on several channels at once, so posting it where others can see it is a disclosure event.

The reconciliation is that **the viewer for an answer is its audience, not its asker**. That is a strictly stronger rule than the one it replaces, and it is what makes the chosen default safe.

## Goals / Non-Goals

**Goals:**

- Reach CyberFriend the way people already talk: mention, DM, slash command.
- Make public answers safe by construction rather than by care.
- Support follow-up questions without re-stating context.
- Bound what one person can spend.

**Non-Goals:**

- Voice, images, or composing messages on someone's behalf.
- Autonomous or scheduled answers. Every answer follows a question.
- Slack.

## Decisions

### The viewer for a public answer is the channel's audience

For an answer delivered in channel `C`, a source channel `S` may contribute only if **everyone who can read `C` can also read `S`**. Note the direction: this is not "may the asker read `S`" — it is a containment check between two audiences.

The practical effect is that a public answer can only tell the room things the room could already have found. Someone with broad access asking in a general channel gets a narrower answer than they would in private, which is correct rather than a defect.

Computing it exactly — enumerate `C`'s members, intersect their visibility — is expensive and fragile. The tractable form uses the role sets the permission resolver already computes: `S` is safe for `C` when the set of roles granted read access to `C` is a subset of those granted read access to `S`, with per-member overwrites handled as exceptions. This is derivable from data we already hold, but it is a **different query** from the one `channel-acl` answers, and it needs its own tests rather than reuse of that code path.

Direct messages and ephemeral replies have an audience of one, which makes them the same rule with a trivial audience — not a special case.

### Withholding is disclosed to the asker, privately

Silently returning a narrower answer is its own failure: the asker concludes nothing exists when something does. So when audience scoping removes evidence the asker could themselves have seen, they get a private note that a fuller answer is available to them.

The note goes only to the asker, and the public answer gives no indication anything was withheld — because "there is more here you cannot see" is itself a disclosure that private content exists. This is the same reasoning that made access-blocked and corpus-empty responses byte-identical in the reasoning change.

### Identity comes from the platform, free

In Discord the asker is authenticated by Discord. There is no bearer token and no client-supplied viewer parameter, so the entire class of "caller asserts an identity" problems that the public MCP endpoint has does not exist here.

That makes this surface structurally safer than the MCP endpoint and the better default route to CyberFriend. Content claiming to come from someone else is disregarded; only the authenticated author counts.

### The bot's own output is not corpus input

Answers the bot posts into indexed channels must not be ingested as evidence. Without this the system cites itself, confidence compounds across rounds, and an early mistake becomes a documented fact. It is a small rule that prevents a slow, hard-to-diagnose degradation.

### Acknowledge fast, answer when ready

A reasoning run can take far longer than Discord's interaction deadline, so the surface acknowledges immediately and delivers when complete. A run that ends without an answer must resolve the acknowledgement explicitly — a silent non-answer is indistinguishable from the bot being broken.

### Limits are a requirement, not tuning

Any member can now trigger a reasoning run that costs model calls and tool invocations. Per-person rate and cost limits are specified as behaviour, and they hold across surfaces so that a limit reached via slash command is not bypassed by asking in a DM.

## Risks / Trade-offs

- **Audience scoping is a new permission computation**, and a wrong containment check leaks to a whole channel rather than one person. It is the highest-value test in this change, and it must be tested independently of the asker-visibility path it resembles.
- **Public answers will sometimes look worse than the asker expects.** That is the design working. The private notice is what keeps it from being mistaken for a retrieval failure — without it, users lose trust in the bot rather than understanding the scoping.
- **Role-subset containment is an approximation** of true member-set containment, and per-member channel overwrites are where the two diverge. Overwrites must be handled explicitly; treating the role check as sufficient is the likely source of a real leak.
- **Cost is now driven by user behaviour**, not by our own scheduling. Limits bound it, but a busy server on a reasoning-heavy default would still be expensive; routing most questions to the fixed path matters more here than anywhere else.
- **Conversational context is a small amount of retained state** per thread, and it carries the asker's scope. A follow-up from a different person in the same thread must be re-scoped to them, not answered under the original asker's access.
