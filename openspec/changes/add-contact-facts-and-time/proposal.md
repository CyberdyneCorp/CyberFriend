## Why

Two gaps, found by asking.

**The assistant remembers three things about a person** — preferred name, email,
preferred language. Somebody who wants to say "my wallet is 0x…" so they can
later ask "what's my balance" cannot, and has to paste the address every time.
A phone number cannot be kept at all.

**The assistant does not know what day it is.** Nothing puts the current time
in any prompt. "What did people ask me today" works because SQL does the
filtering, but the model reasoning about an answer has no anchor for "recent",
"last week" or "still open", and asked the date outright it would answer from
whatever its training data suggests.

## What Changes

- **Three more facts a person may set**: phone number, Ethereum-compatible
  wallet, Bitcoin wallet. Each validated for shape before it is stored.
- **A saved wallet can be used.** "What's my balance?" looks up the address the
  person saved, rather than refusing for want of one in the question.
- **The assistant is told the current time**, in UTC, in every answering prompt.

Non-goals:

- **Storing anybody else's details.** A fact is set from the person's own
  message about themselves, unchanged.
- **Wallets as identity.** A saved address is a convenience for that person's
  own lookups. Nothing resolves an address back to a person, and no answer
  says whose a wallet is.
- **Timezones per person.** One clock, stated as UTC, so an answer that names a
  time says which one.

## Capabilities

### Modified Capabilities

- `personal-facts`: three more kinds, and the rule about a saved value being
  usable for that person's own outbound lookup.

### New Capabilities

- `temporal-awareness`: what the assistant is told about the current time, and
  what it must not do with it.

## Risk

The wallet change touches the outbound boundary, and is the part worth
reviewing rather than the new columns.

An outbound query must be rooted in the asker's words, and a saved address is
not in the question they just typed. The rule is not being relaxed: the root
set is widened from "the words of this question" to "the words this person
wrote about themselves", and a saved fact qualifies because they typed it when
they set it. The check remains containment rather than a label — a caller must
supply the exact values the store holds for that person, so nothing a model or
a message produced can pass through it.

What this does mean is that a person's saved address now leaves the server when
they ask about their own balance. That is what they asked for by saving it, and
it is still their address and their question.
