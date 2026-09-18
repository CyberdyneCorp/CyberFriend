## Why

A channel can be archived with `/index` and un-archived with `/unindex`, and
there is no way to ask what is archived now. The only listing lives in the
admin console, behind an operator token.

So the way to find out whether a channel is indexed is to run `/index` on it
and read the refusal. That is a poor answer to a fair question, and the
question matters: an archive of what people said is a thing they are owed
disclosure about, and disclosure nobody can check is not disclosure.

## What Changes

- **A command lists the archived channels**, scoped to what the asker can read,
  answered privately.

Non-goals:

- **Revealing that other archived channels exist.** The reply says nothing at
  all about channels the asker cannot read -- not their names, not their
  number, not that there are any.
- **Changing scope.** Listing is a read; `/index` and `/unindex` remain the
  only way to change what is archived.
- **Showing how much is stored.** Message counts per channel are an operator's
  question and live in the console.

## Capabilities

### New Capabilities

- `channel-listing`: who can see which channels are archived, and what the
  answer may not disclose.

### Modified Capabilities

None. Scope itself is unchanged; this only reads it.

## Risk

A list of indexed channels is a list of channel names, and naming a channel
tells somebody it exists. The whole design of this feature is the scoping: the
set returned is the archived channels intersected with the asker's own readable
channels, using the same predicate that scopes retrieval. Anything else --
including a count of what was filtered out -- would make the reply a directory
of private channels.
