## Context

This builds directly on `add-discord-chat-memory`: the corpus, the permission filter, and the MCP tool surface already exist. What changes is that answers stop being a single retrieval and start being a process, and that the process gains tools reaching outside Discord.

The design below leans heavily on the Athena team's production experience with a corrective-RAG platform. Where their findings contradicted the obvious approach, their version won; those points are marked. Where their platform genuinely does not cover our case — conversational chunking, measured tool-count degradation — that is stated rather than papered over.

The governing constraint is security, not capability. CyberFriend holds private channel content, ingests text written by anyone in the server, and would now be able to act on external systems. Those three together mean that without deliberate structure, any server member can post a message that steers the bot's tools. Several decisions below cost performance or flexibility and exist only to break that chain.

## Goals / Non-Goals

**Goals:**

- Answer multi-step questions correctly, without making every question pay for it.
- Reach external systems through their existing MCP servers rather than bespoke integrations.
- Make it structurally impossible for indexed content to become instruction or authority.
- Be able to demonstrate that the loop is better than single-shot, not merely slower.

**Non-Goals:**

- Autonomous action. Every run starts with a person asking something.
- Web search as a corrective action — declared-but-unimplemented in Athena for 18 months, and an egress boundary we do not want.
- Graph retrieval. Entity extraction over chat fragments is materially worse than over documents; Athena's own verification fell back to synthetic entities because real extraction was too costly.
- A second answer endpoint for the agentic path. One answer contract, two ways of producing it.

## Decisions

### Two tiers, and the default tier is not agentic

The common questions — *"what did people ask me today"*, *"what happened in #infra"* — are a retrieval plus a filter. Making them agentic buys planning latency and nondeterminism on the path that carries nearly all the traffic.

So: a **fixed path** handles routine questions (`retrieve → evaluate → {answer | correct → retrieve} → answer`), and a classifier routes to a **reasoning loop** only when a question has separable sub-goals or needs an external system. Both produce the same answer contract.

*Athena's strongest recommendation, from having built both: their genuinely agentic tier is a separate deployable, and the fixed path carries almost all real traffic.*

### Authorization is separated from intent, not guarded within it

The strongest version of this invariant is not a policy check — it is a type boundary. Change 1 therefore keeps the viewer's readable-channel predicate off the query object entirely, so that a corrective action rewriting a query has no field through which to reach it.

The same separation governs the policy layer here: **two predicates, sourced from two objects, neither able to edit the other, combined conjunctively.** Preference lives in deployment-editable configuration; permission lives in a collaborator the configuration cannot name or reach. The configuration language has no word for "allowed", so "edit the config to permit X" is not a sentence that can be written.

Note what the guarantee does *not* rest on: the order in which the two checks run. Ordering is an implementation detail and specifying it as the invariant invites a refactor to break it silently. The invariant is the separation.

Two supporting rules:

- **Conditions union, never substitute.** Configuration may add a condition to an action, making it harder; it can never remove one, and inherent conditions always apply.
- **The condition vocabulary is a closed set, not an expression language** — so configuration is validated when it loads rather than failing mid-request. Resisting an expression language here is what keeps this a policy table instead of an unvalidatable workflow engine.

*Credit: Athena, including their correction that filter relaxation in their own system is protected by convention rather than enforcement — which is precisely the failure this design is shaped to avoid.*

### The critic emits an enum; a pure function chooses the action

The evaluator judges evidence and returns one of a small enumerated set of verdicts plus a score. A **deterministic policy** maps verdict to corrective action (reformulate, widen, retry against a different index). The model never chooses the next step — it only scores evidence.

This is the difference between a loop you can unit-test without a model in the room and one you can only observe. A critic emitting prose leaves nothing to route on.

### Two independent bounds, deliberately redundant

`max_attempts` is enforced by the **driver**, not by the policy — a misconfigured policy must not be able to widen its own budget. Any framework-level recursion limit underneath is **derived** from that number rather than hardcoded, because a hardcoded framework limit silently caps a raised application limit and surfaces as a recursion error instead of the extra attempts you asked for.

A corrective round that yields no net-new evidence counts as lack of progress and ends the run rather than spending another attempt on an equivalent query.

### A cheap signal gates the expensive one

Where a cross-encoder reranker has already scored candidates, that score answers "is any of this relevant?" for free. Athena measured the gate at **−38% wall-clock and −43% prompt tokens** on an unanswerable question, reaching an identical outcome — because the system had been paying a model several seconds to conclude what scores three orders of magnitude below the answerable range already showed.

Two guards make this safe, both enforced at startup:

- The gate is valid **only with a calibrated cross-encoder**. A lexical-overlap scorer also emits values in [0,1] but measures term rarity, not answerability; thresholds against it would wave through every lexically-similar-but-irrelevant passage. Boot fails if the configured reranker is not calibrated.
- Scores from different methods are different numbers. This is why change 1 records score provenance: RRF's `0.016` is *first place*, and a threshold applied without checking provenance is a silent misjudgement.

Short Discord messages make cross-encoder inference cheap, so the latency argument against reranking is weaker here than in a document corpus.

### The agent holds no database credential

The reasoning loop reaches the corpus only through change 1's MCP interface — the same viewer-scoped, permission-filtered surface any other client uses. It gets no connection string, no SQL tool, no shell, no filesystem.

This is the structural containment for prompt injection: a crafted message cannot reach rows the permission-filtered interface would not have returned anyway. It converts "the agent must be careful" into "the agent cannot".

*Adopted from Athena, where the agent process likewise holds no storage credential.*

### Federated tools: allowlist, route, re-check

Three separate controls, because each fails differently:

- **Discovery is not registration.** Only explicitly listed tools are available; a server that adds a tool tomorrow gains the agent nothing. A listed tool that no server provides is a startup error, not a silent reduction in capability.
- **Routing** exposes a bounded, relevant subset per run rather than every tool from every server.
- **Invoke-time re-check**, because a model can emit a tool name it was never shown.

Honest caveat: Athena's tool scoping is least-privilege reasoning, not the output of an ablation over tool count. Treat "route rather than expose all" as a well-founded prior here. Measuring the degradation curve is a cheap experiment and would be genuinely new information.

### Mutating tools are off, then confirmed, then argument-bound

Read-only by default; a tool whose effect cannot be determined is treated as mutating. An enabled mutating tool is proposed to the requesting person with its exact arguments and invoked only on explicit confirmation — and if the arguments change after confirmation, confirmation is sought again. Text resembling confirmation inside retrieved content never satisfies this.

The friction is the point. Removing it is a decision to be made explicitly, with its consequences understood.

### Capability validation at boot

With an OpenAI-compatible endpoint that may front self-hosted Qwen3, structured-output and tool-calling reliability vary by serving stack — vLLM, llama.cpp and Ollama differ materially. The composition root checks the configured model against what the enabled stages require and **refuses to start**, naming the missing capability.

The alternative is malformed JSON on request #400, in production, at an unrelated moment.

A second, cheaper handle on the same provider serves per-candidate scoring, which fires once per surviving candidate and needs none of the deliberation the main judge does.

### Calibrating the relevance gate

The gate's thresholds are the part most likely to be set wrongly, because the obvious calibration — relevant versus irrelevant — produces a threshold that confidently waves through half-answers.

Calibration uses **four buckets**: fully covered, **partially covered**, uncovered-but-topically-adjacent, and uncovered-and-far. The two middle buckets carry the whole method:

- The high threshold must clear the **partial** maximum, not the uncovered maximum. Partial evidence scores high — it genuinely answers part of the question — and a gate calibrated against uncovered data will pass it as complete.
- Topically-adjacent-but-absent content produces the high outliers that a naive gate mistakes for relevance.

Three practices come with it:

- **Write the specific data point a threshold exists to exclude into the comment beside it.** Athena raised a threshold to 0.97 *because* a partial-coverage query scored 0.9735 — and 0.97 does not clear 0.9735, so the fix never covered its own case. Without the number recorded next to the threshold, this recurs.
- **State the statistical bound rather than implying one.** Zero failures observed at n=12 bounds the true rate at roughly 25% at 95% confidence, by the rule of three. That is a clean sweep, not a validated margin, and the code should say so.
- **Calibrate against the distribution production actually sees.** Scoring a full corpus per query produces systematically higher scores than scoring the fused survivors of retrieval. A threshold tuned on the harder distribution has headroom against the easier one — which argues for lower thresholds, not higher.

For our corpus specifically, Athena's numbers should not be copied. Their covered-bucket floor drops sharply for very short passages, and they flagged it as a length effect rather than a relevance miss — **our entire corpus is that outlier**. Expect a much lower floor and far more spread, and expect the high band to be unusable. The gate worth building is the low one: when every candidate scores far below the answerable range, skip the expensive evaluation. Its failure mode is paying for a model call that could have been skipped, which is the right direction to fail.

Because window size changes the score distribution, the gate is calibrated *after* windowing parameters settle, or it is calibrated twice.

### A per-item skip against a shared budget can starve later items

If refinement allocates a shared context budget greedily across sources, a document the gate skips still costs its full unrefined size — so one over-costed early item can push later, still-relevant ones out of the budget entirely. The risk exists only when refinement and the gate are both enabled; neither alone produces it.

This is a startup check spanning the three settings involved — maximum sources × expected maximum chunk size against the context budget — not a comment.

## Risks / Trade-offs

- **Prompt injection is the defining risk**, and defence in depth is the only honest answer: content fenced as data, no credentials on the agent, read-only default, confirmation for mutations, and an audit trail. Each layer is individually defeatable; the combination is what holds. Nobody should describe this as solved.
- **Cost and latency rise several-fold** on the loop path. Mitigated by routing most traffic to the fixed path, and by the cheap gate short-circuiting the expensive stage.
- **Evaluation must score abstention as success.** If "I found nothing" counts as failure, tuning drives the bot toward confabulation — Athena's all-stages-off configuration is the fastest and answers questions about Australia with five confident citations to passages about vector databases.
- **Provenance fields lie if untested.** Athena has a known case reporting a decision as model-made when no model call occurred. The provenance record needs its own tests or it will mislead exactly when relied upon.
- **Federated credentials are a prerequisite, not a detail.** Ambient org-wide write credentials on the bot mean every server member wields them through it. Either per-viewer authorization or a narrow read-only identity.
- **The classifier is a new failure surface.** Misrouting a hard question to the fixed path yields a shallow answer; misrouting an easy one wastes money. It needs its own eval set.
