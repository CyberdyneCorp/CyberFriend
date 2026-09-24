## Why

In production this DM saved the full name and nothing else:

> Oi Me chamo Leonardo Araujo dos Santos pode me Chamar de Leo, tenho 45 anos
> nasci em 21/06/1981 meu telefone e +5521980703795 morro no Rio de Janeiro
> Brasil, Vargem Grande minha walet e 0xB26B…75e0 meu email
> leonardoaraujo.santos@gmail.com

A clause ran to the next recognised starter, and "tenho", "nasci" and "morro"
were not starters: the preferred name became "Leo, tenho 45 anos nasci em
21/06/1981" (implausible, so dropped) and the phone became the number plus the
address (refused as too long). "walet" was not a wallet word, and "meu email X"
without "é" was not a statement. Where somebody lives and when they were born
were not kept at all, and a second wallet replaced the first -- deleting the
alerts that watched it.

## What Changes

- **Robust introductions.** More clause starters (`tenho N anos`, `nasci`,
  `nascido`, `moro`, `morro` before a preposition, `i was born`, `born on`,
  `i am N`). A name stops at the first comma, sentence period or connector
  (`e` / `and`; a full name keeps `e` before a capitalised word, as in "Araujo
  e Silva"). A phone or a birth date is the prefix of the value with its
  shape, not the whole clause.
- **Typos and missing verbs.** `walet`, `wallet`, `carteira`; `morro`/`moro`;
  `meu email X`, `my email X`, `meu telefone X`, `minha carteira 0x…` when the
  value has the right shape.
- **Home address** (`home_address`): free text, at most 200 characters, no
  markdown or mention syntax. "moro em …", "I live in …", "my address is …".
  Where somebody lives is no longer reported as "not kept".
- **Birth date** (`birth_date`): `dd/mm/yyyy`, `dd-mm-yyyy`, `yyyy-mm-dd`,
  "21 de junho de 1981", "June 21 1981"; stored ISO; impossible, pre-1900 and
  future dates refused. An age is not kept: it follows from the birth date,
  and the reply says so.
- **Several wallets.** Up to 5 Ethereum and 5 Bitcoin addresses per person.
  Saving one adds it (idempotently); a sixth is refused with how to make room.
  "forget my wallet 0x…" removes that one, "forget my wallets" removes all.
  The portfolio sums every saved wallet; a balance, DeFi or alert question
  that reads one wallet asks which (listing the last four characters) when
  several are saved and the question names none.
- **Privacy.** Address and birth date are direct-message-only, like email and
  phone: shown only to their owner in a DM, in the DM prompt only, and a
  channel message stating them is withheld from the corpus.

## Capabilities

### Modified Capabilities

- `personal-facts`: new kinds, several wallets, robust introductions.

## Risk

More clause starters means more ways to split a message; the reply repeats back
exactly what was saved, so a wrong split is visible at once. "morro" is also
Portuguese for "hill" and "I die", so it starts a clause only before `no`, `na`
or `em`. A home address is free text and is the most sensitive fact kept; it is
bounded, markup-free, never shown outside a DM and never archived from a
channel.
