## Why

One Portuguese DM in production hit four misses in a row:

- "Guarde na sua memoria que eu quero os meus valores em reais brasileiro"
  saved nothing: the "guarde (na sua memória) que" wrapper, "os meus valores"
  and "reais brasileiro" were each unrecognised, so it went to the corpus.
- "Oque vide sabe sobre mim ?" ("vide" for "você") went to the corpus.
- "Você sabe qual é a moeda do meu país ?" went to the corpus.
- After "verifique as pools de liquidez nesta wallet 0xB26B…45e0" was answered,
  a follow-up about the value in the asker's currency was answered "Qual
  carteira?", and "A carteira que eu acabei de passar" was searched for in the
  corpus.

## What Changes

- **Remember wrappers.** "guarde / grave / anote / lembre / salve / registre
  (na sua memória) que …" and "remember that …" before any fact statement.
- **Currency phrasing.** "(eu) quero (ver) os (meus) valores em X", "I want to
  see my prices in X"; the real in every number and gender ("reais
  brasileiro", "real brasileiros", "brazilian reals").
- **Show.** "o que / oque / oq <one word> sabe sobre mim" and "what do <one
  word> know about me" when the word is "você" in any spelling or a short
  lowercase typo of it; a capitalised name or another pronoun is not the
  assistant. "Você sabe qual …" / "do you know what …" before a fact asked
  for by name.
- **The currency asked for.** "qual a moeda do meu país", "qual a minha moeda
  de base", "what's the currency of my country", "which currency do I use"
  show the preferred currency; with none saved the reply says so and how to
  save one ("minha moeda é o real").
- **The wallet just given.** A chain question (balance, positions, portfolio,
  activity, alert) that needs an address and has none typed, carried or saved
  reads the latest address the asker typed in this conversation -- their own
  remembered questions, kept per person and location -- and logs its source
  as `asker_typed`. A message that only points back at a wallet ("a carteira
  que eu acabei de passar", "essa carteira", "the wallet I just gave you")
  asks the latest chain question again with that address. The chain call
  budget is charged per question as well as per address, so that follow-up
  is not refused as the earlier run calling again.

## Impact

- `app/routing.py`, `app/routing_crypto.py`, `app/reasoning/service.py`,
  `app/alert_intent.py`, `app/alert_requests.py`, `app/fact_replies.py`,
  `domain/currency.py`, `adapters/chain/clearance.py`.
- No schema change. Egress rooting is unchanged: the address is spelled into
  the question, and it is the asker's own words.
