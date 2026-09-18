## Why

Every useful question has to be asked. "What did people ask me today", "what
happened in #infra", "what does my wallet hold" are all questions somebody
wants the answer to on a rhythm, and the only way to get one is to remember to
ask.

## What Changes

- **Scheduled tasks.** A person schedules a question to be asked on their
  behalf, between once an hour and once a day, and is sent the answer in a
  direct message.
- **Commands to list, create and delete them**, each person seeing and
  changing only their own.
- **Silence when there is nothing.** A run that finds nothing sends nothing.
- **The listing shows when each task last ran**, because a silent task and a
  broken one are otherwise the same thing.

Non-goals:

- **Posting into a channel.** A scheduled answer goes to the person who asked
  for it. Unprompted channel messages are a different feature with a different
  audience problem, and this one does not open that door.
- **Acting on anything.** A scheduled run has no one to approve a
  state-changing tool, so every one is refused. That is the existing rule, not
  a new one.
- **Backfilling missed runs.** A task that could not run at its time runs next
  time, once. Replaying a missed window is how a restart floods somebody.
- **Sub-hourly schedules.** The floor is an hour, and it is a cost control as
  much as a courtesy.

## Capabilities

### New Capabilities

- `scheduled-tasks`: what a person may schedule, when it runs, what it may
  reach, and what stops it.

### Modified Capabilities

None. Answering, scoping, egress and confirmation are all unchanged — a
scheduled run goes through the same `AskService` a typed question does.

## Risk

This is the second feature that sends a message nobody asked for in the
moment, and it is far more open-ended than the first. The notification queue is
bounded by the shape of an obligation; a scheduled task is bounded only by what
a person typed and how often they chose.

Three things carry that weight, and all three are why the design looks as it
does: a per-person cap on how many tasks may exist, silence when there is
nothing to say, and the same undeliverable handling that stops the assistant
messaging somebody whose direct messages are closed.

The fourth is not a limit but a consequence worth stating plainly: a scheduled
run can reach the external tools, so this deployment will make outbound calls
on a timer with nobody watching. Each is still bounded per run by the existing
budget, rate limit and egress guard, and still rooted in the words the person
typed — but "a person is present" stops being true, and any reasoning that
relied on it no longer holds.
