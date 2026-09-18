## Where the scheduler runs

The bot, not `ingest`.

`ingest` is where every other sweep lives, and the instinct is to put this one
there too. It cannot go there: running a task means answering a question, and
the answer stack -- retrieval, the reasoning loop, the federated tools, the ACL
resolver over live guild state -- is built in the bot process. `ingest` has none
of it, and giving it a second copy would mean two processes that must agree
about what a person may read.

Delivery is a direct message, which only the bot can send. So both halves are
already here, and the scheduler is a loop beside the notification drain rather
than a new seam between processes. Both processes are pinned to one replica,
and the claim below is atomic regardless.

## Claiming, and why the schedule advances first

    UPDATE scheduled_task
       SET next_run_at = now() + interval, last_claimed_at = now()
     WHERE next_run_at <= now() AND disabled_at IS NULL
     RETURNING ...

The advance happens in the claiming statement, before the run. A crash midway
through a task then costs one answer rather than looping on it, which is the
right way round: the task runs again next interval anyway.

`now() + interval` rather than `next_run_at + interval` is what makes a missed
window skipped instead of replayed. If the bot is down for six hours, an hourly
task's `next_run_at` is six hours in the past; advancing by one interval at a
time would run it six times on restart and send six messages. The cost is drift
-- a task creeps later by however long a run takes -- and for an hourly rhythm
that is seconds a day and nobody's problem.

## What a run is

`AskService.ask(AskRequest(asker=person, text=task.text, destination=None), None)`.

Deliberately the same entry point a typed question uses, with two consequences
worth naming because they are the whole safety story and neither is new code:

**Access is resolved now.** The ACL resolver reads live guild state for the
asker at the moment of the run, so a task created when somebody could read
`#infra` stops drawing on `#infra` the moment they cannot. Nothing about the
stored task needs to know this happened.

**Nothing can act.** The second argument is the confirmation surface, and there
is nobody to show a prompt to, so it is None. `AskService._attending` already
says what that means: "Missing either, nothing attends and every mutating call
is refused for want of a confirmation". A scheduled run therefore cannot invoke
a state-changing tool, by the existing rule rather than a new check.

`destination=None` means the answer is composed for a private audience, which
is correct: it is going to that person's direct messages and nowhere else.

## Silence, and what it costs

A run that abstains sends nothing. An hourly task reporting "I found nothing"
twenty-four times a day is what makes somebody mute the bot -- and muting it
also silences the obligation notifications they actually need, so the cost of
being chatty here is paid by a different feature.

The price is that a working-but-quiet task is indistinguishable from a broken
one. That is why the listing is not optional decoration: every task shows when
it last ran and what happened, so "it has run eleven times and found nothing"
is a thing a person can see. Without that, silence would be a bug report
waiting to happen.

## Rate limiting

`AskService` already rate-limits per person, and a scheduled run must not spend
that allowance: a person whose five tasks just fired should not find their own
next question refused. Scheduled runs are counted separately, against the
task's own schedule -- which is itself the limit, since a task can only run
once an interval.

## What this costs the deployment

Every run is a full reasoning run: retrieval, at least one model call, possibly
tool calls. The arithmetic is worth doing before enabling it -- `people ×
tasks × 24` is the daily ceiling for hourly tasks, and each one is the cost of
a question nobody typed.

Two bounds keep it finite: the per-person task cap, and the hour floor. Neither
is a budget. A deployment that wants a spending limit needs one, and this
change does not pretend to provide it.

## Deletion

A task is deleted when its owner deletes it, and when the person's data is
removed. The second is the one that is easy to forget: opt-out and erasure
purge facts, memory and notifications today, and a scheduled task that outlived
them would keep messaging somebody who asked to be gone. It hangs off the same
person row with `ON DELETE CASCADE`, so the purge that exists already takes it.
