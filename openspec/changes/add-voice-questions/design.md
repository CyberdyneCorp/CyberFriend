## Context

`CyberFriendClient.on_message` answered an empty DM with the capabilities text,
so a voice message (content empty, one `audio/ogg` attachment, flag
`IS_VOICE_MESSAGE`) never reached the ask path. discord.py exposes the
attachment's `content_type`, `size`, `url` and `duration` (Discord's
`duration_secs`, set on voice messages only).

## Decisions

### Where it lives

The bot process only. `on_message` diverts an empty DM that carries audio to
`_answer_voice`, which asks `VoiceQuestions.hear` for a transcript or a refusal
and then calls the same `_answer` a typed DM goes through, with the quoted line
as a prefix. Nothing downstream knows the question was spoken: routing,
memory, the progress note and the withheld notice are the typed path.

### The order of checks

Each step can only refuse, and nothing later runs after a refusal:

1. Metadata: exactly one attachment; declared type in
   {`audio/ogg`, `audio/mpeg`, `audio/mp4`, `audio/wav`, `audio/webm`}; URL
   `https` on `cdn.discordapp.com` or `media.discordapp.net`; `size` within
   `VOICE_MAX_BYTES`; declared duration within `VOICE_MAX_SECONDS`.
2. Ledger: opted out, over the person's cap, over the deployment's cap -- else
   charge. One transaction under `pg_advisory_xact_lock(hashtext('media_usage'))`.
3. Download through `BoundedHttpFetcher(transport=edges.http_transport,
   follow_redirects=False)`, abandoned past the byte cap; a 3xx is a failure.
4. Sniff: `OggS`, `ID3` or an MPEG frame sync, `ftyp` at offset 4,
   `RIFF....WAVE`, EBML `1A 45 DF A3`. The sniffed type must equal the declared
   one.
5. Transcribe; flatten whitespace; empty is a failure.

Steps 3-5 fail as one refusal, `UNHEARD`. A failing ledger fails closed as
`UNHEARD` too: without it there is no cap.

### Charging

By the declared duration rounded up, before download, and not refunded. The
caps bound what can be sent to the endpoint; refunding on a failed download
would make them depend on where the failure happened. A clip with no declared
duration is charged `VOICE_MAX_SECONDS`, so an unknown length never slips under
a cap it would not fit.

### The ledger

`media_usage(person_id → person ON DELETE CASCADE, month date, purpose
'question'|'channel', seconds)`, primary key on the first three. The person cap
sums the person's `question` rows for the month; the deployment cap sums every
row for the month. `channel` is reserved so channel transcription (PR 9) shares
the ceiling without a constraint migration. An asker never seen in a channel is
given a person row, as other stores do. Opt-out keeps the rows: they carry no
content, and dropping them would return the month's minutes to the ceiling.

### The transport

Both outbound calls use `Edges.http_transport`, so the end-to-end harness's
`FakeWeb` answers them and its seal refuses any real connection. The
transcriber is plain httpx rather than the OpenAI SDK for that reason. Hosts
are closed: the CDN pair is a constant, and the transcription host is the one
configured URL; redirects are followed by neither.

### Replies

One fixed reply per refusal (`VOICE_REPLIES`), in Portuguese when the person's
saved language is Portuguese, else English -- the rule the capabilities reply
already follows, since a message carries no client locale. The person and
server caps have separate replies because the person can act on the
difference. The quoted line is `-# 🎤 "…"`, markdown-escaped and cut at about
200 characters on a word boundary.

### Privacy

- Only the asker's own DM, never a channel.
- The audio is never persisted; the transcript is stored only as the question
  in conversation memory, like typed text, and obeys `/forget`, retention and
  opt-out as that does.
- Logs carry the refusal reason and the transcript length, never the words.
