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

1. Metadata: exactly one attachment; declared type `audio/ogg`; URL
   `https` on `cdn.discordapp.com` or `media.discordapp.net`; `size` within
   `VOICE_MAX_BYTES`; declared duration within `VOICE_MAX_SECONDS`.
2. Ledger: opted out, over the person's cap, over the deployment's cap -- else
   charge. One transaction under `pg_advisory_xact_lock(hashtext('media_usage'))`.
3. Download through `BoundedHttpFetcher(transport=edges.http_transport,
   follow_redirects=False)`, abandoned past the byte cap; a 3xx is a failure.
4. Measure: `app/ogg_opus.py` parses the bytes as one whole Ogg stream of
   Opus packets (`OpusHead`, `OpusTags`, then audio) and sums each packet's
   frames from its TOC byte (RFC 6716 3.1). Longer than `VOICE_MAX_SECONDS`
   is `TOO_LONG`; longer than the seconds charged, or not Ogg Opus at all, is
   `UNHEARD`. Nothing is sent in either case.
5. Transcribe; flatten whitespace; empty is a failure, over 4000 characters
   is `TOO_LONG`.

Steps 3-5 otherwise fail as one refusal, `UNHEARD`. A failing ledger fails
closed as `UNHEARD` too: without it there is no cap.

Before step 1 runs at all, the asker's hourly question allowance is read
without being spent; a person already at it gets the question-limit reply and
nothing is charged or fetched.

### Charging

By the declared duration rounded up, before download, and not refunded. The
caps bound what can be sent to the endpoint; refunding on a failed download
would make them depend on where the failure happened. A clip with no declared
duration is charged `VOICE_MAX_SECONDS`, so an unknown length never slips under
a cap it would not fit.

The declared duration is `duration_secs`, written by the uploading client: a
modified client can declare one second for ten minutes. So the charge is only
half the bound. The other half is step 4: the audio is counted from its own
packets and refused if it is longer than it was charged. What is sent is
never longer than what was charged, and what was charged fitted both caps.

### Why only Ogg Opus

The count has to come from the bytes a decoder will play, not from a header
the sender also wrote (an Ogg granule position, an MP4 `mvhd`, a WAV byte
rate). Opus states every packet's duration in its first byte, so the count is
exact and small to implement, and it is what a Discord voice message is.
MP3, M4A, WAV and WebM uploads are refused as unsupported until each has an
equally honest count. Counting errs long: an empty packet or a code-3 packet
missing its frame count is counted as 120 ms, the most a packet can hold.

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
