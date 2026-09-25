## 1. Recognising the language

- [x] 1.1 Detect the language of a question, for the languages spoken here
- [x] 1.2 Return "unknown" rather than guessing on a short or mixed question
- [x] 1.3 Test: Portuguese and English questions are told apart
- [x] 1.4 Test: an unrecognisable question yields no answer language

## 2. Answering in it

- [x] 2.1 Instruct synthesis to answer in the question's language
- [x] 2.2 A saved preferred language still takes precedence
- [x] 2.3 Do not translate cited content
- [x] 2.4 Test: a preferred language beats the question's language

## 3. The capability reply

- [x] 3.1 Render it in the question's language
- [x] 3.2 List every available command with what it does
- [x] 3.3 Omit a command whose feature is switched off
- [x] 3.4 Name capabilities in human terms, not by server identifier
- [x] 3.5 Test: asked in Portuguese, answered in Portuguese
- [x] 3.6 Test: every command the bot registers appears in the reply

## 4. Wiring and documentation

- [x] 4.1 Pass the available commands from the surface that registers them
- [x] 4.2 Assert the command list cannot drift from what is registered
- [x] 4.3 README and docs

## 5. Live

- [ ] 5.1 Deploy
- [ ] 5.2 Confirm a Portuguese question is answered in Portuguese

## 6. Complete capabilities

- [x] 6.1 Describe every running feature in short sections, EN and PT, each
      with example questions, each shown only where its feature is on
- [x] 6.2 One `Capabilities` value built from settings and registered tools,
      shared by "what can you do?" and a bare mention
- [x] 6.3 Keep every message within Discord's 2000-character limit, split
      between sections
- [x] 6.4 Test: a table of setting -> phrase, with everything on and each off
- [x] 6.5 Test: the alert, fact, catch-up and decision examples reach their routes
- [x] 6.6 E2E: a Portuguese DM with features on lists crypto, alerts,
      decisions and voice, and a switched-off feature is not mentioned
