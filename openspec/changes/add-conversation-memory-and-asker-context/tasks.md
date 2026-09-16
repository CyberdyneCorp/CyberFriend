## 1. Schema

- [ ] 1.1 Migration: `conversation_turn` (person, location, question, answer, cited channel ids, created_at) and `conversation_summary` (person, location, text, covered channel ids, through_turn, created_at)
- [ ] 1.2 Delete-cascade from `person`, and purge on opt-out through the existing mechanism
- [ ] 1.3 Register every statement in the SQL audit with a written reason

## 2. Memory store

- [ ] 2.1 Store a turn with its question, answer and cited channel ids
- [ ] 2.2 Load turns and summaries for (person, location), newest last
- [ ] 2.3 Filter turns and summaries by the person's CURRENT readable channels before returning them
- [ ] 2.4 Test: a turn drawing on a revoked channel is not returned
- [ ] 2.5 Test: a summary touching one revoked channel is not returned at all
- [ ] 2.6 Test: one person's turns never appear for another person in the same channel
- [ ] 2.7 Test: DM history never appears for a channel question

## 3. Summarisation

- [ ] 3.1 Summarise older turns past a configured bound, keeping the most recent verbatim
- [ ] 3.2 Record the union of covered channel ids on the summary
- [ ] 3.3 Run it on the cheap model, off the answering path, so a question is never slowed by it

## 4. Asker context

- [ ] 4.1 Resolve display name, nickname and role names for the asker from the gateway cache
- [ ] 4.2 Fence the profile as data in the prompt
- [ ] 4.3 Test: an instruction in a nickname has no effect
- [ ] 4.4 Test: the prompt contains role information for the asker and nobody else

## 5. Reasoning

- [ ] 5.1 Pass permitted memory into the planner and synthesizer as fenced context
- [ ] 5.2 Test: a remembered answer is never cited
- [ ] 5.3 Test: a follow-up is answered from fresh retrieval, not remembered text
- [ ] 5.4 Replace the in-memory `ConversationStore` so nothing is still keyed by channel alone

## 6. Control

- [ ] 6.1 `/forget` deletes the person's history and summaries in that location, or everywhere
- [ ] 6.2 Retention sweep deletes turns and summaries past the window
- [ ] 6.3 Declare new settings in docker-compose.yml and test_compose_env.py

## 7. Verification

- [ ] 7.1 Live: ask a question, then a follow-up that only makes sense with context
- [ ] 7.2 Live: restart the bot between them; the follow-up still resolves
- [ ] 7.3 Live: `/forget`, then the follow-up no longer resolves
