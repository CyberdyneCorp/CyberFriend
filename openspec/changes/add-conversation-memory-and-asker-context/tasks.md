## 1. Schema

- [x] 1.1 Migration: `conversation_turn` (person, location, question, answer, cited channel ids, created_at) and `conversation_summary` (person, location, text, covered channel ids, through_turn, created_at)
- [x] 1.2 Delete-cascade from `person`, and purge on opt-out through the existing mechanism
- [x] 1.3 Register every statement in the SQL audit with a written reason

## 2. Memory store

- [x] 2.1 Store a turn with its question, answer and cited channel ids
- [x] 2.2 Load turns and summaries for (person, location), newest last
- [x] 2.3 Filter turns and summaries by the person's CURRENT readable channels before returning them
- [x] 2.4 Test: a turn drawing on a revoked channel is not returned
- [x] 2.5 Test: a summary touching one revoked channel is not returned at all
- [x] 2.6 Test: one person's turns never appear for another person in the same channel
- [x] 2.7 Test: DM history never appears for a channel question

## 3. Summarisation

- [x] 3.1 Summarise older turns past a configured bound, keeping the most recent verbatim
- [x] 3.2 Record the union of covered channel ids on the summary
- [x] 3.3 Run it on the cheap model, off the answering path, so a question is never slowed by it

## 4. Asker context

- [x] 4.1 Resolve display name, nickname and role names for the asker from the gateway cache
- [x] 4.2 Fence the profile as data in the prompt
- [x] 4.3 Test: an instruction in a nickname has no effect
- [x] 4.4 Test: the prompt contains role information for the asker and nobody else

## 5. Reasoning

- [x] 5.1 Pass permitted memory into the planner and synthesizer as fenced context
- [x] 5.2 Test: a remembered answer is never cited
- [x] 5.3 Test: a follow-up is answered from fresh retrieval, not remembered text
- [x] 5.4 Replace the in-memory `ConversationStore` so nothing is still keyed by channel alone

## 6. Control

- [x] 6.1 `/forget` deletes the person's history and summaries in that location, or everywhere
- [x] 6.2 Retention sweep deletes turns and summaries past the window
- [x] 6.3 Declare new settings in docker-compose.yml and test_compose_env.py

## 7. Verification

- [ ] 7.1 Live: ask a question, then a follow-up that only makes sense with context
- [ ] 7.2 Live: restart the bot between them; the follow-up still resolves
- [ ] 7.3 Live: `/forget`, then the follow-up no longer resolves
