## ADDED Requirements

### Requirement: A home address and a birth date are kept as facts

The system SHALL store a home address as bounded free text without markup, and
a birth date as an ISO date parsed from the common written forms, and SHALL
refuse an impossible, pre-1900 or future birth date.

#### Scenario: Where somebody lives
- WHEN someone writes "moro no Rio de Janeiro Brasil, Vargem Grande"
- THEN "Rio de Janeiro Brasil, Vargem Grande" SHALL be stored as their home address

#### Scenario: A written birth date
- WHEN someone writes "nasci em 21/06/1981" or "I was born on June 21 1981"
- THEN 1981-06-21 SHALL be stored as their birth date

#### Scenario: A birthplace is not a birth date
- WHEN someone writes "nasci em São Paulo" or "I was born in London"
- THEN no birth date SHALL be stored, and in an introduction the reply SHALL name
  where they are from as not kept

#### Scenario: Loose phrases are not facts
- WHEN someone writes "I live in fear of liquidation", "subi o morro no domingo"
  or "I'm 100% sure"
- THEN no home address SHALL be stored and no age SHALL be named

#### Scenario: A value asked as a question
- WHEN someone writes "minha carteira 0x…?" or "my email x@y.com?" without "é"/"is"
- THEN nothing SHALL be stored and the message SHALL be answered as a question

#### Scenario: "My address" alone
- WHEN someone asks "what is my address?" or "qual é o meu endereço?"
- THEN it SHALL be answered as a question, which in a DM may use both the home
  address and the wallets; "my home address" / "meu endereço residencial" SHALL
  show the home address

#### Scenario: A future birth date
- WHEN someone gives a birth date after today
- THEN it SHALL NOT be stored and the reply SHALL say it is not a date it can read

### Requirement: An age is not kept

The system SHALL NOT store an age, and SHALL say so in the reply to an
introduction, explaining that it follows from the birth date when one was given.

#### Scenario: Age beside a birth date
- WHEN an introduction says "tenho 45 anos" and gives a birth date
- THEN the reply SHALL say the age is not kept because it follows from the birth date

### Requirement: An introduction saves every fact it states, with typos

The system SHALL recognise each fact in an introduction even when a value is
followed by further words, a wallet is spelled "walet", "morro" is written for
"moro", or an email, phone or wallet is given without "é"/"is".

#### Scenario: The production introduction
- WHEN a DM reads "Oi Me chamo Leonardo Araujo dos Santos pode me Chamar de
  Leo, tenho 45 anos nasci em 21/06/1981 meu telefone e +5521980703795 morro
  no Rio de Janeiro Brasil, Vargem Grande minha walet e 0xB26B…75e0 meu email
  leonardoaraujo.santos@gmail.com"
- THEN the full name, preferred name "Leo", phone, home address, birth date,
  Ethereum wallet and email SHALL all be stored
- AND the reply SHALL list each saved item in Portuguese

### Requirement: A person may save several wallets

The system SHALL keep up to 5 wallets of each chain per person, SHALL treat
saving a held wallet as a no-op, SHALL refuse a sixth with how to make room, and
SHALL let one wallet be forgotten by its address or all of them at once.

#### Scenario: A second wallet
- WHEN someone with a saved wallet saves another
- THEN both SHALL be stored and the alerts on the first SHALL be kept

#### Scenario: Forgetting one wallet
- WHEN someone writes "esqueça minha carteira 0x…" naming one of their wallets
- THEN only that wallet and the alerts that relied on it being saved SHALL be deleted

#### Scenario: Forgetting "my wallet" with several saved
- WHEN someone with two saved wallets writes "esqueça minha carteira" without naming one
- THEN nothing SHALL be deleted and the reply SHALL ask which, listing each wallet's
  last four characters
- AND "esqueça minha carteira …45e0" SHALL then delete only the wallet ending in 45e0

#### Scenario: Forgetting every wallet
- WHEN someone writes "forget my wallets" or "esqueça minhas carteiras"
- THEN every saved Ethereum and Bitcoin wallet SHALL be deleted

### Requirement: A lookup that reads one wallet asks which of several

The system SHALL sum every saved wallet for a portfolio question, and for a
question that reads one wallet SHALL use the saved wallet the question names by
its last characters, or else ask which one, listing only each wallet's last
four characters.

#### Scenario: Balance with two saved wallets
- WHEN someone with two saved wallets asks "qual o saldo da minha carteira?"
- THEN nothing SHALL be read and the reply SHALL ask which wallet

### Requirement: Address and birth date are shown only in a direct message

The system SHALL show a home address and a birth date only to their owner in a
direct message, SHALL include them in the prompt only in a direct message, and
SHALL withhold from the corpus a channel message in which the sender states
their own home address or birth date.

#### Scenario: Introduction in a channel
- WHEN the production introduction is sent in a channel
- THEN every fact SHALL be stored, and the reply SHALL confirm the address,
  birth date, phone, email and wallet without their values
- AND the message SHALL NOT be stored in the corpus

#### Scenario: A short age before a contact detail
- WHEN a channel message reads "I'm 45, my email is a@b.com"
- THEN the message SHALL NOT be stored in the corpus
