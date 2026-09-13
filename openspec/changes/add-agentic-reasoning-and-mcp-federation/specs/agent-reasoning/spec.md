## Purpose

Answer multi-step questions by planning them into sub-questions, retrieving iteratively, judging whether the evidence gathered is sufficient, and stopping within explicit budgets — rather than returning whatever a single retrieval pass happened to surface.

## ADDED Requirements

### Requirement: Query planning

The system SHALL decompose a question into the steps needed to answer it, and SHALL execute a question requiring several distinct lookups as several retrievals rather than one.

#### Scenario: Question requiring multiple lookups
- WHEN a viewer asks a question that depends on more than one distinct piece of evidence
- THEN the system SHALL perform a separate retrieval for each
- AND SHALL combine the results into a single answer

#### Scenario: Simple lookup
- WHEN a viewer asks a question answerable by a single retrieval
- THEN the system SHALL NOT expand it into additional retrievals

### Requirement: Sufficiency assessment and refinement

The system SHALL judge whether retrieved evidence is sufficient to answer the question, and SHALL reformulate and retry when it is not.

#### Scenario: Initial retrieval returns nothing useful
- GIVEN a first retrieval returns no results, or results irrelevant to the question
- WHEN the system assesses sufficiency
- THEN it SHALL reformulate the query and retrieve again, within its remaining budget

#### Scenario: Evidence remains insufficient after refinement
- WHEN the system exhausts its refinement budget without sufficient evidence
- THEN it SHALL state that it could not find an answer
- AND SHALL NOT present unsupported assertions as findings

#### Scenario: Evidence sufficient on first attempt
- WHEN the first retrieval returns evidence sufficient to answer
- THEN the system SHALL proceed to answer without further retrieval

### Requirement: Bounded termination

Every reasoning run SHALL terminate within configured limits on reasoning steps, token consumption, and wall-clock time, and SHALL do so whatever the question or tool behaviour.

#### Scenario: Step budget exhausted
- WHEN a run reaches its configured maximum number of reasoning steps
- THEN the system SHALL stop and answer from the evidence gathered so far
- AND SHALL indicate that the answer is partial

#### Scenario: Time budget exhausted
- WHEN a run reaches its configured wall-clock limit
- THEN the system SHALL stop and return a partial answer or an explicit failure

#### Scenario: Repeating without progress
- WHEN the system issues a retrieval substantially equivalent to one it has already issued in the same run
- THEN it SHALL treat that as a lack of progress and SHALL NOT repeat it indefinitely

### Requirement: Grounded answers

Every factual claim in an answer SHALL be traceable to retrieved evidence, and answers SHALL carry the citations for that evidence.

#### Scenario: Answer derived from retrieved messages
- WHEN the system answers from retrieved evidence
- THEN the answer SHALL include citations resolving to the source messages

#### Scenario: Question the corpus cannot answer
- WHEN no evidence supports an answer
- THEN the system SHALL say so rather than answering from the model's own knowledge

### Requirement: Permission scope preserved across the run

Every retrieval performed during a reasoning run SHALL be scoped to the person who asked, for the whole run.

#### Scenario: Multi-step run by a restricted viewer
- GIVEN a viewer who cannot read a given channel
- WHEN the system performs any retrieval at any step of a run initiated by that viewer
- THEN no content from that channel SHALL enter the run's context, be used to form a later step, or appear in the answer

#### Scenario: Broadening the search after weak results
- GIVEN a retrieval returned insufficient evidence
- WHEN the system broadens its search as a corrective action
- THEN it MAY broaden the query, the number of results requested, or the time range
- AND it SHALL NOT broaden the set of channels searched beyond what the requesting person may read

### Requirement: Observable reasoning

The system SHALL record the steps taken during a run — queries issued, tools called, and evidence used — so an answer can be explained and debugged after the fact.

#### Scenario: Run completes
- WHEN a reasoning run completes
- THEN the system SHALL have recorded its steps, the retrievals issued, and the evidence that informed the answer

### Requirement: Routing between a fixed path and a reasoning loop

The system SHALL classify an incoming question and answer it by a fixed retrieval path unless the question requires steps that path cannot serve, so that routine questions do not pay the latency, cost, and variability of a reasoning loop.

#### Scenario: Routine question
- WHEN a question can be answered by retrieval and a filter over its results
- THEN the system SHALL answer it by the fixed path
- AND SHALL NOT invoke the reasoning loop

#### Scenario: Question requiring separable sub-goals
- WHEN a question requires evidence from several distinct lines of enquiry, or from an external system
- THEN the system SHALL route it to the reasoning loop

#### Scenario: Both paths answer the same contract
- WHEN a question is answered by either path
- THEN the answer SHALL carry the same structure, citations, and permission guarantees

### Requirement: Constrained evaluation verdicts

The component that judges retrieved evidence SHALL emit a verdict drawn from a fixed, enumerated set, and the choice of corrective action SHALL be determined from that verdict by a rule the model does not participate in.

#### Scenario: Evidence judged
- WHEN the system evaluates retrieved evidence
- THEN the verdict SHALL be one of an enumerated set of outcomes
- AND SHALL NOT be free-form text interpreted as an instruction

#### Scenario: Corrective action selected
- WHEN a verdict indicates the evidence is insufficient
- THEN the corrective action SHALL be determined from the verdict by a deterministic rule
- AND the same verdict SHALL always yield the same action

### Requirement: Budgets enforced independently of policy

The limits on a run SHALL be enforced by the component that drives the run, independently of the configuration that selects corrective actions, so that a misconfigured policy cannot extend a run beyond its budget.

#### Scenario: Policy requests further work past the limit
- GIVEN a run has reached its configured maximum number of attempts
- WHEN the corrective policy indicates a further attempt
- THEN the system SHALL stop

#### Scenario: Corrective round yields no new evidence
- WHEN a corrective retrieval returns no evidence beyond what the run already holds
- THEN the system SHALL treat the run as having stopped making progress
- AND SHALL NOT spend a further attempt on an equivalent retrieval

### Requirement: Abstention is a successful outcome

Reporting that there is no answer SHALL be a successful result, distinct from a failure, so that a genuine absence of activity is reported accurately rather than filled in.

#### Scenario: Question with no matching activity
- WHEN a viewer asks about activity that did not occur
- THEN the system SHALL report that it found nothing
- AND that response SHALL be recorded as a successful outcome, not an error

#### Scenario: Retrieval dependency unavailable
- WHEN the system cannot complete retrieval because a dependency failed
- THEN it SHALL report a failure, distinguishable from having found nothing

### Requirement: Decision provenance

The system SHALL record what determined each significant decision in a run, so that a change in behaviour can be identified as an improvement or a regression.

#### Scenario: Decision made without consulting a model
- WHEN the system concludes that evidence is insufficient using a cheap signal rather than a model call
- THEN it SHALL record that the decision was made by that signal
- AND the recorded count of model calls for that decision SHALL be zero

#### Scenario: Decision made by a model
- WHEN a model call determines a decision
- THEN the record SHALL attribute the decision to the model

### Requirement: Terminal outcomes distinguish cause for operators without disclosing it to requesters

The system SHALL record why a run produced no answer in enough detail for an operator to act, while the response shown to the requesting person SHALL NOT reveal the existence of content they may not see.

#### Scenario: No answer because the corpus holds nothing
- WHEN a run finds no relevant evidence in content the requester may read, and nothing was withheld
- THEN the recorded outcome SHALL identify the corpus as the cause

#### Scenario: No answer because relevant content was outside the requester's access
- WHEN a run finds no answer, and relevant content exists that the requester may not read
- THEN the recorded outcome SHALL identify access as the cause, for operators
- AND the response to the requester SHALL be indistinguishable from the case where no such content exists

#### Scenario: Access-blocked and corpus-empty runs are not otherwise distinguishable
- WHEN a run produces no answer because relevant content was outside the requester's access
- THEN it SHALL follow the same path as a run that found nothing in an accessible corpus
- AND SHALL NOT short-circuit ahead of that path, or emit any observable signal the other does not

#### Scenario: No answer because a corrective action was unavailable
- WHEN a run ends because a required action was not permitted by configuration
- THEN the recorded outcome SHALL identify configuration as the cause, so an operator can tell a policy problem from a corpus problem

### Requirement: Configuration may restrict actions but never authorise them

The configuration that selects corrective actions SHALL be able to make an action harder to perform but SHALL NOT be able to make a disallowed action permitted. Permission SHALL be determined by a separate mechanism that the configuration cannot name or reach.

#### Scenario: Configuration adds a condition to an action
- WHEN configuration attaches an additional condition to an action
- THEN the action SHALL require both its inherent conditions and the added one

#### Scenario: Configuration attempts to remove an inherent condition
- WHEN configuration would remove a condition inherent to an action
- THEN that condition SHALL still apply

#### Scenario: Configuration referencing permission
- WHEN configuration is written
- THEN it SHALL have no means of expressing that an action is permitted

### Requirement: Every run has an action that can always be taken

The system SHALL guarantee at load time that, from any state a run can reach, at least one action requires no conditions, so that no run can reach a state with nothing it may do.

#### Scenario: Configuration with no unconditional fallback
- WHEN configuration is loaded in which some reachable state offers only conditional actions
- THEN the system SHALL reject that configuration at load time rather than fail during a run
