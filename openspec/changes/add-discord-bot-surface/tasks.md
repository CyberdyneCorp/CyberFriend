## 1. Audience resolution

- [x] 1.1 Implement audience resolution for a destination: the set of people who can read a given channel, and the trivial single-person audience for DMs and ephemeral replies
- [x] 1.2 Implement the containment check — source channel `S` is permitted for destination `C` when everyone who can read `C` can also read `S`
- [x] 1.3 Handle per-member channel overwrites explicitly; a role-subset check alone is where a real leak would come from
- [x] 1.4 Test containment independently of asker-visibility — they resemble each other and reusing that code path is the likely defect
- [x] 1.5 Test: asker with broad access asking in a general channel gets an answer containing no content that channel's audience cannot read

## 2. Answer scoping and delivery

- [x] 2.1 Scope retrieval for an answer by its destination audience, applied as the same storage-layer filter used for asker scoping
- [x] 2.2 Implement the private notice when audience scoping removed evidence the asker could themselves have seen
- [x] 2.3 Ensure the public answer gives no indication that anything was withheld
- [ ] 2.4 Re-scope or refuse an answer whose delivery destination changes after it was produced
- [x] 2.5 Test: public answer evidence is always a subset of the private answer's evidence for the same question and asker
- [x] 2.6 Test: a request that asks for broader scope, and retrieved content that directs broader disclosure, both change nothing

## 3. Discord surface

- [x] 3.1 Register the slash command and implement mention and DM handling
- [x] 3.2 Take the asker from the authenticated message author, never from message content
- [x] 3.3 Ignore messages in indexed channels that do not address the bot
- [x] 3.4 Respond to a bare mention with capabilities rather than attempting an answer
- [x] 3.5 Render answers with citation links that resolve to the source messages
- [x] 3.6 Acknowledge within Discord's interaction deadline and deliver the answer when ready
- [ ] 3.7 Resolve the acknowledgement explicitly when a run ends without an answer

## 4. Conversation

- [x] 4.1 Maintain per-thread and per-DM conversational context for follow-ups
- [x] 4.2 Re-scope to the new asker when a different person continues a conversation
- [x] 4.3 Expire context after a configured idle period and treat later messages as new questions
- [x] 4.4 Test: a follow-up from a lower-access person does not inherit the original asker's scope

## 5. Limits

- [x] 5.1 Implement per-person rate and cost limits
- [x] 5.2 Enforce limits across surfaces so a DM does not bypass a slash-command limit
- [ ] 5.3 Let in-flight answers complete when a limit is reached, and decline new ones with a clear message
- [ ] 5.4 Record per-question cost so spend is attributable

## 6. Corpus hygiene

- [ ] 6.1 Exclude the bot's own messages from ingestion
- [ ] 6.2 Test: an answer posted into an indexed channel is not citable in a later answer

## 7. Verification

- [ ] 7.1 In a scratch guild with a private channel, confirm a public answer to a privileged asker omits private evidence while the private answer includes it
- [ ] 7.2 Confirm the withheld-evidence notice reaches only the asker
- [ ] 7.3 Confirm a follow-up from a restricted member is answered under their own scope
- [ ] 7.4 Confirm rate limits hold across mention, DM and slash command
