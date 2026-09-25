## ADDED Requirements

### Requirement: Voice questions are off by default

The system SHALL NOT download or transcribe any audio unless
`VOICE_QUESTIONS_ENABLED` is set, and SHALL refuse to start with it set and no
`MEDIA_API_KEY`.

#### Scenario: Switched off
- WHEN a person sends the bot a voice message in a DM and voice questions are
  off
- THEN the system SHALL reply once that voice is not enabled and to type the
  question, in the person's saved language
- AND SHALL reach no external host and make no model call

#### Scenario: Enabled without a key
- WHEN `VOICE_QUESTIONS_ENABLED` is true and `MEDIA_API_KEY` is absent
- THEN the bot SHALL fail at startup naming the missing setting

### Requirement: Only a single allowlisted Discord attachment is heard

The system SHALL hear a voice question only from exactly one attachment whose
declared type is `audio/ogg`, `audio/mpeg`, `audio/mp4`, `audio/wav` or
`audio/webm`, served over https from `cdn.discordapp.com` or
`media.discordapp.net`, within `VOICE_MAX_BYTES` and, when declared,
`VOICE_MAX_SECONDS`, and SHALL decide this before charging or downloading.

#### Scenario: A URL off the Discord CDN
- WHEN the attachment's URL names any other host
- THEN the system SHALL reply that it cannot listen to that file
- AND SHALL NOT fetch the URL

#### Scenario: Too long
- WHEN the declared duration exceeds `VOICE_MAX_SECONDS` or the size exceeds
  `VOICE_MAX_BYTES`
- THEN the system SHALL reply with the limit and download nothing

#### Scenario: More than one attachment
- WHEN the message carries more than one attachment
- THEN the system SHALL ask for one voice message at a time

#### Scenario: Bytes that disagree with the declared type
- WHEN the downloaded bytes do not carry the magic of the declared audio type
- THEN the system SHALL NOT send them for transcription and SHALL reply that
  it could not understand the audio

### Requirement: Monthly caps are checked and charged before download

The system SHALL charge each voice question its declared duration (rounded up,
or `VOICE_MAX_SECONDS` when none is declared) against the person's monthly cap
and the deployment's monthly cap in one transaction, before anything is
downloaded, and SHALL refuse without charging when either would be exceeded.

#### Scenario: Over the personal cap
- WHEN a person's voice questions this month plus this one exceed
  `VOICE_PERSON_MONTHLY_MINUTES`
- THEN the system SHALL reply that their voice allowance for the month is used
- AND SHALL NOT download the audio or call the transcription endpoint

#### Scenario: Over the deployment cap
- WHEN all audio transcribed this month plus this one exceeds
  `MEDIA_AUDIO_MONTHLY_MINUTES`
- THEN the system SHALL reply that the server's limit is reached, without a
  download or a transcription call

#### Scenario: A new month
- WHEN a calendar month (UTC) begins
- THEN both caps SHALL count from zero

### Requirement: An opted-out person is not transcribed

The system SHALL refuse a voice question from a person recorded in the
personal-data opt-out, without downloading or transcribing it.

#### Scenario: Opted out
- WHEN an opted-out person sends a voice message
- THEN the system SHALL reply that it does not transcribe their voice and to
  type the question

### Requirement: A transcript is answered exactly as typed text

The system SHALL answer the transcript through the same path as a typed direct
message, and SHALL open the reply with a small quoted line of what was
understood.

#### Scenario: A Portuguese voice question
- WHEN a person asks "o que o Leo disse sobre o deploy semana passada?" by voice
- THEN the reply SHALL begin with `-# 🎤 "o que o Leo disse sobre o deploy
  semana passada?"`
- AND the answer SHALL be the one the typed question gets, in Portuguese, cited
- AND the transcript SHALL be remembered as the question

#### Scenario: Nothing understood
- WHEN the download fails, the endpoint fails, or the transcript is empty
- THEN the system SHALL reply that it could not understand the audio and SHALL
  NOT raise

### Requirement: Audio is never stored

The system SHALL NOT persist the audio or any derivative of it other than the
transcript used as the question, and SHALL log no transcript text.

#### Scenario: After an answer
- WHEN a voice question has been answered
- THEN no table SHALL hold the audio, and `media_usage` SHALL hold only the
  seconds charged
