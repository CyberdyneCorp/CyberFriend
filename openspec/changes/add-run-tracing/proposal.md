## Why

Nobody can say whether the assistant is getting better. A run logs its shape --
path, status, cause, spend -- and discards the three things you would need to
improve it: the question as asked, the answer as sent, and the evidence the
answer was written from. When someone says "it got that wrong", there is
nothing left to look at.

`RunRecord` was built for an operator watching a live system, and deliberately
carries no content. That is the right call for a log line and the wrong one for
study. What is missing is a second, narrower destination that does carry
content -- and, because it does, inherits every obligation the corpus has.

## What Changes

- **Traces.** Each run may be recorded to an external tracing service: the
  question, the answer, the decision trail already in `RunRecord`, and the
  retrieved evidence behind the answer.
- **Off unless chosen.** Tracing is disabled by default and sends nothing until
  an operator configures a destination, as with every other outbound boundary.
- **Deletion reaches the traces.** A message removed from the corpus takes the
  exported evidence quoting it with it.

Non-goals:

- **Changing an answer.** Tracing observes; it never feeds a run. A tracing
  failure is never visible to the person asking.
- **A second permission system.** Traces are not viewer-scoped and are not
  served to requesters. They are operator-facing, and the destination is
  trusted at the level of the whole corpus.
- **Scoring or evaluation.** This records what happened. Judging it is separate.

## Capabilities

### New Capabilities

- `tracing`: what a run exports for later study, when, and what it owes the
  content it copies.

### Modified Capabilities

None. `RunRecord` and its logging sink are unchanged; tracing is an additional
port alongside them.

## Risk

The trace store holds verbatim private-channel content with no viewer scoping.
Anyone who can read it can read everything the assistant has retrieved, across
every channel. This is a deliberate operator decision, not a default: the
destination must be treated as carrying the same confidentiality as the
database, and access to it restricted the same way.
