## ADDED Requirements

### Requirement: A preferred currency is kept as a fact

The system SHALL store a preferred currency as the ISO 4217 code of a currency
the FX rate source publishes, read from its name in Portuguese or English or
from its code, and SHALL refuse any other currency with a reply, in the
asker's language, that lists the supported codes.

#### Scenario: Said on its own
- WHEN someone writes "minha moeda é o real brasileiro", "prefiro ver em
  reais", "moeda preferida: BRL" or "my currency is euro"
- THEN BRL (or EUR) SHALL be stored as their preferred currency, and the reply
  SHALL name the currency in the language they wrote in

#### Scenario: Inside an introduction
- WHEN a DM reads "Oi, me chamo Leo, moro no Brasil e uso reais"
- THEN BRL SHALL be stored as the preferred currency alongside the other facts,
  and the reply SHALL list it as saved

#### Scenario: A currency the source does not publish
- WHEN someone writes "minha moeda é o peso argentino"
- THEN nothing SHALL be stored, and the reply SHALL say it is not a currency
  that can be converted and list the supported codes

#### Scenario: A clause that runs on inside an introduction
- WHEN a DM reads "Oi, me chamo Leo e uso reais no dia a dia" or "Oi, me chamo
  Leo, moro no Brasil e uso reais. Qual o preço do bitcoin?"
- THEN BRL SHALL be stored alongside the other facts, not dropped

#### Scenario: Words near a currency are not a preference
- WHEN someone writes "uso o Discord", "prefiro ver em português" or "quanto é
  100 dólares em reais?"
- THEN no preferred currency SHALL be stored

#### Scenario: Using a code or a coin is not a preference
- WHEN someone writes "I use PHP", "uso CAD", "eu uso bitcoin" or "Hi, my name
  is Leo and I use PHP"
- THEN no preferred currency SHALL be stored and no currency refusal SHALL be
  sent; a code is read only where only money is meant ("prefiro ver em BRL",
  "moeda preferida: BRL"), and a coin is refused only when named as the
  currency outright ("minha moeda é bitcoin")

#### Scenario: Shown, replaced and forgotten
- WHEN someone asks "o que você sabe sobre mim?", states another currency, or
  writes "esqueça minha moeda"
- THEN the currency SHALL be listed by name and code, replaced, or deleted
  respectively; `/forget` everywhere SHALL delete it with every other fact

#### Scenario: Not private
- WHEN the preferred currency is asked for in a channel
- THEN it SHALL be shown there, as the preferred language is
