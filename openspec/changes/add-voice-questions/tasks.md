## PR 10a. Voice questions in DMs

- [x] 1.1 Settings: `VOICE_QUESTIONS_ENABLED`, `VOICE_MAX_SECONDS`, `VOICE_MAX_BYTES`, `VOICE_PERSON_MONTHLY_MINUTES`, `MEDIA_AUDIO_MONTHLY_MINUTES`, `MEDIA_BASE_URL`, `MEDIA_API_KEY`, `MEDIA_AUDIO_MODEL`, `MEDIA_TIMEOUT_SECONDS`; positive-value validators; enabling without a key fails at boot; compose declares them for the bot only
- [x] 1.2 `app/voice.py`: metadata checks, charge arithmetic, the measured length, `VoiceQuestions.hear`, localised fixed replies
- [x] 1.3 Migration 0025 `media_usage`; `PostgresVoiceLedger` with opt-out, both caps and the charge in one locked transaction; SQL audit registrations
- [x] 1.4 `OpenAICompatibleTranscriber` over `Edges.http_transport`, redirects off; `BoundedHttpFetcher(follow_redirects=False)`
- [x] 1.5 `on_message` diverts DM audio to `_answer_voice`; the transcript takes the typed path with the `-# 🎤` line; wiring through `build_voice_questions` -> `build_bot(voice=...)`
- [x] 1.6 Capabilities reply and self-description mention voice (EN/PT) only where enabled
- [x] 1.7 Unit (limits, allowlists, Opus length, caps, adapter request/parse, reply languages), integration (ledger caps, month, opt-out, cascade) and e2e (PT voice answered in PT with the quoted transcript; off -> fixed reply and no host; over the personal cap -> no download or call; non-CDN URL never fetched)
- [x] 1.8 README, docs/operations.md, docs/deploy-coolify.md
- [x] 1.9 Live check against the real endpoint with a recorded PT voice note, once an operator enables it (confirmed working in production by the operator on 2026-09-25)
- [x] 1.10 Review: count the real length from Opus packets and refuse audio longer than the limit or the charge (voice is Ogg Opus only); cap the transcript at 4000 characters; read the rate limit before transcribing; tests for concurrent charges, the redirect guard, the byte bound and the DM-only boundary
