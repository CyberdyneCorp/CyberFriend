## Why

In production, "Oi meu nome e Leonardo Araujo dos Santos, pode me chamar de
Leo, meu email e ..., moro no Brasil, Rio de Janeiro, meu telefone e ..." saved
nothing. The parser expects one fact per message, so the introduction became an
ordinary question: it was answered from channel messages, and the whole text --
email and phone included -- was stored as a remembered conversation turn and
sent to the model. It was also not withheld from the corpus: only a message that
is *entirely* an email statement was.

Separately, the full name that was asked for alongside the preferred name was
never added, and "oque voce sabe sobre mim" (the common joined spelling) was
not recognised as asking what the assistant knows.

## What Changes

- **Several facts in one message.** A message is split where each fact starts
  ("meu", "my", "pode me chamar", "call me", "moro", "I live"...), and each part
  is recognised on its own. Everything recognised is saved, and one reply says
  what was saved and what was not.
- **Full name.** A new fact kind, `full_name`. "My name is X" / "meu nome é X"
  with two or more words is a full name; with one word it stays the preferred
  name, as before. "My full name is" / "meu nome completo é" is always a full
  name. It is a name, not contact data: it may be used in answers like the
  preferred name, and the preferred name is still what the person is called.
- **Where someone lives is said, not stored.** It is not one of the facts; the
  reply names it as not saved instead of the message falling to the corpus.
- **Contact details never enter the corpus.** A channel message stating the
  sender's own email *or phone*, alone or within an introduction, is withheld
  from ingest.
- **"oque"** is read as "o que" when asking what the assistant knows.
- **Confirmation wording.** A saved phone or wallet is confirmed as a phone or
  wallet; it was confirmed as "your email address".

## Capabilities

### Modified Capabilities

- `personal-facts`: introductions, full name, contact withholding.

## Risk

The split is on fact lead words, so a value containing one ("call me my lord")
would be cut; the reply repeats back exactly what was saved, so a wrong split
is visible immediately and can be corrected. Nothing new is sent anywhere.
