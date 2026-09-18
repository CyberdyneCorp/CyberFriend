## Why

Asked "Oque voce pode fazer? Quais as suas funcionalidades e comandos?", the
assistant replied in English. This server speaks Portuguese, and the person
asking had just written a whole sentence of it.

A preferred language can be saved as a personal fact, and when it is, the
answer follows it. But that is a setting somebody has to know exists. With no
setting, nothing says which language to write in — so the answer comes back in
whatever the prompt happened to be written in.

The same reply also listed one command out of six, and named two servers by
their internal identifiers (`chain_balances`, `context7`) rather than saying
what they do.

## What Changes

- **Answers match the question.** An answer is written in the language the
  question was asked in. A saved preferred language still wins, because that is
  a choice somebody made rather than one inferred for them.
- **The capability reply is localised** rather than a fixed English string.
- **Every command is listed**, with what it does.
- **Capabilities are named in human terms**, not by server identifier.

Non-goals:

- **Translating retrieved content.** A quoted message is shown as it was
  written; translating evidence would make a citation not match its source.
- **Detecting every language.** Two are recognised, because two are spoken
  here. Anything unrecognised is answered as the model would have answered
  anyway, which is the behaviour being replaced rather than a regression.
- **Per-channel or per-server language settings.**

## Capabilities

### New Capabilities

- `answer-language`: which language an answer is written in, and what decides
  it.
- `self-description`: what the assistant says about itself when asked, and
  where that answer comes from.

### Modified Capabilities

None. `personal-facts` already specifies that a saved preferred language is
honoured, and that is unchanged — this adds what happens when none is saved.
