## Why

People tell an assistant how they want to be addressed and expect it to stick.
"Call me Leo" or "my email is ..." is currently answered like any other
question: it is searched for in the channels, nothing is found, and it is gone
by the next message. Conversation memory does not help, because it expires,
is kept per channel, and is context for interpreting a follow-up rather than a
record of facts about the person.

Answers are also plain text. Discord renders markdown, and a price, a list of
decisions, or a code snippet from documentation reads far better with bold
figures, bullets, code blocks and small source lines than as one paragraph.

## What Changes

- **Personal facts.** A person can tell the assistant their preferred name,
  email address and preferred language, change them, see them, and delete
  them. The assistant uses the preferred name when addressing them and replies
  in their preferred language.
- **Rich formatting.** Answers use Discord markdown: bold for key figures,
  lists, code blocks, and sources as small subtext lines with links.

Non-goals:

- **Facts about other people.** A person can only set facts about themselves,
  and only by saying so directly to the assistant. Nothing is extracted from
  channel messages, and no one can ask for another person's facts.
- **Arbitrary notes.** The set of facts is fixed. A free-form "remember this"
  store would become a place to plant text that later reaches every prompt.

## Capabilities

### New Capabilities

- `personal-facts`: the facts a person may set about themselves, who may see
  them, and where they may appear.
- `rich-formatting`: how answers are rendered in Discord, and which parts of
  an answer may carry formatting or links.

### Modified Capabilities

None. `asker-context` already gives the model the asker's Discord profile;
personal facts are added alongside it as data.

## Impact

- **An email address is personal data**, stored until the person deletes it.
  It is never shown in a channel, only in a direct message to its owner, and
  it is covered by `/forget` and the existing opt-out.
- **Markdown turns quoted content into a phishing surface.** A message quoting
  `[your account](https://evil.example)` would render as a disguised clickable
  link once answers are rendered as markdown. Only links the assistant generates
  for citations may be masked; any other link in an answer is shown as its bare
  address.
- **A preferred name is text the person chose**, and it reaches the prompt.
  It is fenced as data and length-bounded.
