## 1. Answer contract and routing

- [x] 1.1 Define one answer contract shared by both paths: answer text, citations, partial/abstained flags, decision provenance, budget consumed
- [x] 1.2 Implement the question classifier deciding fixed path vs reasoning loop
- [x] 1.3 Build a labelled classifier eval set and record its confusion matrix — misrouting is a new failure surface
- [x] 1.4 Test: both paths return the identical contract shape for the same question

## 2. Fixed corrective path

- [x] 2.1 Implement the fixed topology: `retrieve → evaluate → {answer | correct → retrieve} → answer`
- [x] 2.2 Implement the evaluator returning an **enumerated verdict plus score** — never free-form text
- [x] 2.3 Implement the corrective policy as a pure function mapping verdict to action (reformulate, widen top-k, widen time range)
- [x] 2.4 Test the policy exhaustively **with no model involved** — this is the payoff of the enum verdict
- [x] 2.5 Enforce that widening may change query, result count, or time range, and can never change the channel set — relying on change 1's type boundary (the predicate is not a field on the rewritable query), not on a policy check
- [x] 2.6 Implement the permission check as a separate collaborator the policy configuration cannot name or reach; ANDed with executability, with the **separation** as the tested invariant rather than the check order
- [x] 2.7 Enforce that configured conditions union with inherent ones and can never remove them
- [x] 2.8 Make the condition vocabulary a closed enum, not an expression language, so configuration validates at load rather than failing mid-request
- [x] 2.9 Validate at load that every reachable state offers at least one unconditional action; reject the configuration otherwise
- [x] 2.10 Implement no-progress detection: dedupe new evidence against evidence already held, end the run when a round adds nothing net-new
- [x] 2.11 Track blocked actions alongside executable ones so terminal outcomes distinguish corpus / access / configuration as the cause
- [x] 2.12 Test: the requester-facing response for access-blocked and corpus-empty outcomes is byte-identical, while the operator record distinguishes them
- [x] 2.13 Forbid an early exit for viewers with an empty visible-channel set — it would make the access-blocked path observably faster than a genuine search, reintroducing the disclosure the byte-identical response closes. Assert equivalent observable behaviour, not only equal bytes

## 3. Budgets and termination

- [x] 3.1 Implement driver-enforced `max_attempts`, independent of the corrective policy's configuration
- [x] 3.2 Implement token, wall-clock, model-call and tool-call budgets, injected server-side and not modifiable by the model
- [x] 3.3 Derive any framework recursion limit from `max_attempts` rather than hardcoding it
- [x] 3.4 Test: a policy configured to loop forever still terminates at `max_attempts`
- [x] 3.5 Test: raising `max_attempts` actually yields more attempts and is not silently capped underneath

## 4. Relevance gate

- [ ] 4.1 Integrate a cross-encoder reranker over fused candidates
- [x] 4.2 Implement the cheap gate: short-circuit the LLM evaluation when every candidate scores below threshold
- [x] 4.3 **Refuse to boot** when the gate is enabled and the configured reranker is not calibrated for answerability
- [x] 4.4 Enforce that no threshold is applied to a score without checking the `relevance_source` recorded in change 1
- [ ] 4.5 Benchmark gate on/off on an unanswerable question: record wall-clock, model calls, prompt tokens, and confirm the outcome is unchanged
- [ ] 4.6 Build the calibration corpus with **four buckets** — covered, partially covered, uncovered-adjacent, uncovered-far — where partial is defined for our domain as "one message in the window is on-topic, the rest is noise"
- [ ] 4.7 Score the calibration corpus under **production-matched inference settings** (activation, truncation); a threshold calibrated under different settings belongs to a model we do not run
- [x] 4.8 Set the low threshold above the uncovered-far maximum; if a high threshold is used at all, it must clear the **partial** maximum
- [x] 4.9 Record beside each threshold the specific score it exists to exclude, and the rule-of-three bound on the observed sample — a clean sweep is not a validated margin
- [ ] 4.10 Account for the production distribution shift: calibration scores a larger pool than production's fused survivors, so thresholds tuned on it have headroom and should err low
- [x] 4.11 Expose relevance scores through a single accessor that returns nothing unless the score is present **and** the reranker is calibrated
- [x] 4.12 Read only the reranker's own score — never a fused or fallback score, which is unbounded and would feed an arbitrary number into a probability-calibrated threshold
- [x] 4.13 Keep any evaluation-bypass gate behind its own flag, off by default, not reachable from the low-band primitives
- [x] 4.14 Add the startup check for gate-plus-refinement: max sources × expected max chunk tokens must fit the context budget, since a skipped document still costs its unrefined size
- [ ] 4.15 Calibrate only **after** change 1's windowing parameters settle, since window size moves the score distribution

## 5. Reasoning loop

- [x] 5.1 Implement the loop over the shared answer contract, with planning into sub-questions
- [x] 5.2 Give the loop access to corpus retrieval **only via change 1's MCP interface** — no database credential, no SQL, no shell, no filesystem
- [x] 5.3 Thread the requesting viewer through every step so permission scope holds for the whole run
- [x] 5.4 Implement grounded answer synthesis carrying citations for every claim
- [x] 5.5 Implement abstention as a successful outcome distinct from failure
- [x] 5.6 Test: a restricted viewer's multi-step run never surfaces inaccessible content at any step

## 6. MCP client and federation

- [x] 6.1 Implement the MCP client: connect to configured servers, discover tools, load each server independently so one failure cannot hide healthy tools
- [x] 6.2 Implement allowlist registration — discovery does not confer availability; a listed-but-undiscovered tool is a startup error
- [x] 6.3 Implement server-qualified tool namespacing so same-named tools cannot collide
- [x] 6.4 Implement per-run tool routing to a bounded relevant subset
- [x] 6.5 Implement per-call timeouts, result size limits with truncation marking, and graceful degradation on server failure
- [ ] 6.6 Attribute federated contributions to their originating system in answers
- [x] 6.7 Test: an unreachable server at startup does not prevent operation; a mid-run failure does not become "no information"
- [ ] 6.8 Measure answer quality against federated tool count — the degradation curve Athena could not supply

## 7. Authorization and injection defence

- [x] 7.1 Implement structural fencing so retrieved content and tool results enter context as data with a clear boundary, never as instruction
- [x] 7.2 Implement invoke-time authorization re-check, independent of what was offered to the loop
- [x] 7.3 Implement read-only default; classify effect-unknown tools as mutating
- [x] 7.4 Implement the confirmation flow: present tool, target, and exact arguments to the requester; re-confirm if arguments change
- [x] 7.5 Ensure confirmation can only originate from the requesting person — never from retrieved content or a tool result
- [ ] 7.6 Ensure answers are delivered only to the requester, and that content directing publication elsewhere is disregarded
- [x] 7.7 Implement the append-only audit record: requester, question, evidence in context, tool, arguments, outcome, confirmation state
- [x] 7.8 **Build a prompt-injection test corpus** — indexed messages attempting tool invocation, permission escalation, exfiltration to another channel, and forged confirmation — and assert none produces an action
- [x] 7.9 Test: a refused invocation is recorded with its reason
- [ ] 7.10 Verify the agent process genuinely has no route to the database

## 8. Model capability validation

- [x] 8.1 Define the capability requirements of each enabled stage (structured output, tool calling)
- [x] 8.2 Validate the configured OpenAI-compatible model at boot and **refuse to start**, naming the missing capability
- [x] 8.3 Add a second cheaper handle on the same provider for per-candidate scoring
- [ ] 8.4 Test against at least two serving stacks, since structured-output behaviour differs across them

## 9. Evaluation

- [x] 9.1 Implement deterministic retrieval metrics: recall@k, precision@k, MRR, nDCG@k
- [x] 9.2 Implement abstention correctness as a first-class scored outcome — scoring it as failure tunes the bot into confabulating
- [x] 9.3 Implement run comparison: pin the same dataset version (hard error otherwise), match on stable example id, compare the metric intersection and report what each side had that the other lacked
- [x] 9.4 Report per-example deltas, not aggregates — "helped on 3, hurt on 7" is invisible in a mean
- [x] 9.5 Test the provenance fields themselves: a decision made by the cheap gate must record zero model calls
- [x] 9.6 Record cost and latency per question per path, so the loop's price is visible

## 10. Verification

- [ ] 10.1 Run the injection corpus end-to-end against a live instance with a mutating tool enabled; confirm zero unconfirmed actions
- [ ] 10.2 Ask a multi-step question spanning Discord and one federated server; confirm correct attribution and citations
- [ ] 10.3 Confirm a restricted viewer's loop run cannot surface inaccessible content
- [ ] 10.4 Compare fixed path vs loop on the golden set; confirm the loop wins where routed and record where it does not
- [ ] 10.5 Kill a federated server mid-run and confirm the answer degrades honestly rather than silently
