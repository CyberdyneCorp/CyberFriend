# add-voice-questions

A voice message sent to the bot in a DM is transcribed and answered as if the
words had been typed, with a small quoted line of what was understood above the
answer. Off by default, under hard monthly caps per person and per deployment.

This is PR 10a of the media plan: voice questions only. Transcribing voice
notes posted in channels (PRs 8-9) and describing images (PR 10) are separate
changes; they share this change's settings and its usage table.

- `proposal.md` -- why, what changes, and the risk
- `design.md` -- the order of checks, the ledger, the transport, privacy
- `specs/voice-questions/spec.md` -- the requirements
- `tasks.md` -- progress
