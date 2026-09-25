# add-media-transcription

Voice notes and images posted in indexed channels become text on the message
they were posted with: transcribed, or described and read, then rendered into
that message's conversation window. So citations, the channel ACL, deletion,
retention, opt-out and person/date search apply to them unchanged.

Three PRs, and this change stays open until the last:

- PR 8 -- capture: one pending `message_media` row per allowlisted attachment.
  No downloads, no model calls.
- PR 9 -- voice notes and audio: `MediaWorker`, transcription, post-processing,
  rendering into windows. Off by default.
- PR 10 -- images: description and visible text, a separate switch.

- `proposal.md` -- why, what changes, and the risk
- `design.md` -- the data model, capture rules, privacy
- `specs/channel-media/spec.md` -- the requirements
- `tasks.md` -- progress
