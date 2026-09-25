## Why

People on this server record voice notes as often as they type, and a voice
message sent to the bot today is answered with the capabilities text: the
audio is dropped at the adapter. Asking by voice is a request the team made
directly. It is also the smallest media feature with a clear privacy boundary:
the recording is the asker's own, sent by them to the bot in their own DM, and
answering it needs nothing stored.

## What Changes

- **The switch.** `VOICE_QUESTIONS_ENABLED`, off by default. Off, a voice
  message in a DM gets one fixed reply (voice is not enabled here, please type)
  and nothing is downloaded. On, `MEDIA_API_KEY` is required at boot.
- **Hearing.** A new `app/voice.py` (`VoiceQuestions`, the `Transcriber` and
  `VoiceLedger` ports) checks the attachment from its metadata (one attachment,
  Ogg audio, Discord CDN host, byte and duration limits), then the opt-out and
  the monthly caps, charging the declared duration, and only then downloads
  through the process's HTTP transport, counts the real length from the Opus
  packets (refusing audio longer than the limit or the charge) and
  transcribes.
- **Transcription.** `adapters/llm/transcription.py`: a multipart POST to
  `{MEDIA_BASE_URL}/audio/transcriptions` (default OpenAI,
  `gpt-4o-mini-transcribe`) over `Edges.http_transport`, redirects off.
- **Caps.** Migration 0025 adds `media_usage` (person, month, purpose,
  seconds). `VOICE_PERSON_MONTHLY_MINUTES=60` per person and
  `MEDIA_AUDIO_MONTHLY_MINUTES=1500` for everybody, the latter shared with
  future channel transcription.
- **Answering.** The transcript goes into the ordinary DM ask path unchanged,
  and the reply opens with `-# 🎤 "<transcript, ~200 chars>"`.
- **Self-description.** The capabilities reply and "what can you do?" mention
  voice (EN and PT) only where the switch is on.

Non-goals:

- **Channels.** A voice note mentioning the bot in a channel is not heard;
  channel audio is transcription of other people's speech, a separate change.
- **Storing audio.** Never. Not even a hash.
- **A voice-specific opt-out.** The global personal-data opt-out refuses voice;
  a per-person "don't transcribe my voice notes" preference belongs with channel
  transcription, where the person is not the one choosing to send.
- **Replying by voice.**

## Impact

- Data model: new `media_usage` table; no change to existing tables.
- Egress: a new outbound destination, `MEDIA_BASE_URL`, receiving recordings,
  and downloads from two Discord CDN hosts. Both only when switched on.
- Cost: at most `MEDIA_AUDIO_MONTHLY_MINUTES` of transcription a month, about
  $4.50 at the default and at $0.003/min.
- Settings and compose: nine new bot-only variables.

## Risk

- A recording of someone's voice leaves for a third party with the default
  endpoint. Mitigated by the switch, a separate key and endpoint, and the
  documented option of a self-hosted Whisper.
- A misheard question gets a confident answer to the wrong question. The
  quoted line is there so the person sees what was heard.
- The declared duration is the uploading client's word. It is charged before
  download, and the audio is then counted from its Opus packets and refused
  if longer than the limit or the charge, so a lying client cannot exceed a
  cap. The cost is that only Ogg Opus (Discord voice messages) is heard.
