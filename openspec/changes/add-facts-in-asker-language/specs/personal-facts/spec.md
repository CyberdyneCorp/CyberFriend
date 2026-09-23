## ADDED Requirements

### Requirement: Fact replies are in the asker's language

The system SHALL give fixed replies about personal facts, and its fixed
no-answer reply, in the language the message was written in.

#### Scenario: A Portuguese question about stored facts
- WHEN someone asks "o que você sabe sobre mim?"
- THEN the reply SHALL be in Portuguese

### Requirement: One fact can be asked for

The system SHALL answer a request for one of the asker's facts from the store,
and SHALL NOT search the corpus for it.

#### Scenario: Asking for one's phone in a DM
- WHEN someone asks "qual o meu telefone?" in a direct message
- THEN their stored phone number SHALL be shown

#### Scenario: Asking for one's phone in a channel
- WHEN someone asks for their phone in a channel
- THEN the reply SHALL NOT show it or say whether one is stored

### Requirement: Contact details reach the prompt only in a DM

The system SHALL include the asker's email, phone and wallets in the prompt
for a direct message, and SHALL NOT include them in the prompt for a channel.

#### Scenario: A channel question
- WHEN someone with a stored phone asks a question in a channel
- THEN the prompt SHALL NOT contain the phone

### Requirement: A slash command typed as text is explained

The system SHALL answer a message that is a slash command's name with how to
run it, and SHALL NOT search the corpus for it.

#### Scenario: Typed /forget
- WHEN someone sends "/forget" as a message
- THEN the reply SHALL say to pick /forget from Discord's command menu
