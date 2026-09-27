## ADDED Requirements

### Requirement: Facts are recognised as people phrase them

The system SHALL recognise a fact statement behind a request to remember it,
the currency preference in its common phrasings, and a request to show the
asker's facts with the assistant named in any common spelling or typo, without
reading a question about money or about another person as either.

#### Scenario: A currency kept in memory
- WHEN someone writes "Guarde na sua memoria que eu quero os meus valores em
  reais brasileiro"
- THEN BRL SHALL be stored as their preferred currency

#### Scenario: Money talk is not a preference
- WHEN someone writes "quanto é 100 reais em dólar?" or "quero trocar 100
  reais em dólar"
- THEN no preferred currency SHALL be stored

#### Scenario: "você" typed as people type it
- WHEN someone writes "Oque vide sabe sobre mim ?"
- THEN their facts SHALL be shown, as for "o que você sabe sobre mim?"

#### Scenario: Somebody else is not the assistant
- WHEN someone writes "o que o João sabe sobre mim?" or "o que ele sabe sobre
  mim?"
- THEN their facts SHALL NOT be shown for it

### Requirement: The preferred currency can be asked for

The system SHALL answer "qual a moeda do meu país", "qual a minha moeda",
"what's my currency" and their variants with the preferred currency, and with
none saved SHALL say so and how to save one, in the asker's language.

#### Scenario: None saved
- WHEN someone with no preferred currency writes "Você sabe qual é a moeda do
  meu país ?"
- THEN the reply SHALL say no preferred currency is saved and suggest "minha
  moeda é o real", without searching the corpus
