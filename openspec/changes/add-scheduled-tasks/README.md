# add-scheduled-tasks

Let a person schedule a question to be asked on their behalf, hourly to daily,
and be sent the answer in a direct message. Commands to list, create and delete.

The design rests on two things that are already true rather than new: a
scheduled run goes through the same `AskService` a typed question does, so
access is resolved at run time and every state-changing tool is refused for
want of somebody to approve it.

The judgement call is silence. A run that finds nothing sends nothing, because
an hourly "I found nothing" is what makes people mute the bot -- and muting it
silences the obligation notifications they do need. The cost is that a quiet
task looks broken, which is why the listing showing last run and outcome is
part of the feature rather than a nicety.

- `proposal.md` -- why, what changes, and the risk
- `design.md` -- where it runs, how claiming works, what it costs
- `specs/scheduled-tasks/spec.md` -- the requirements
- `tasks.md` -- the work
