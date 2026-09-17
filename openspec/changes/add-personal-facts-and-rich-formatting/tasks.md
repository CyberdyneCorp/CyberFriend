## 1. Personal facts: storage

- [ ] 1.1 Migration: `person_fact` (person, kind in {preferred_name, email, preferred_language}, value, updated_at), unique per person and kind
- [ ] 1.2 Delete on `/forget` everywhere, on opt-out through the existing trigger, and on person deletion
- [ ] 1.3 Register every statement in the SQL audit with a reason

## 2. Personal facts: behaviour

- [ ] 2.1 Recognise setting, changing, showing and deleting a fact in the asker's own message to the assistant
- [ ] 2.2 Validate email format and bound preferred-name length; confirm every stored fact back to the person
- [ ] 2.3 Refuse facts outside the set, naming what can be remembered
- [ ] 2.4 Never store a fact stated about someone else or found in channel content
- [ ] 2.5 Add facts to the prompt alongside the asker profile, fenced as data
- [ ] 2.6 Address the person by preferred name; reply in preferred language
- [ ] 2.7 Show email only in a direct message to its owner; never disclose or confirm another person's facts
- [ ] 2.8 Test: "João's email is ..." stores nothing
- [ ] 2.9 Test: asking for another member's email discloses nothing and does not confirm existence
- [ ] 2.10 Test: showing your facts in a channel omits the email
- [ ] 2.11 Test: an instruction set as a preferred name has no effect

## 3. Rich formatting

- [ ] 3.1 Synthesiser guidance for Discord markdown: bold figures, lists, code blocks, sources as `-#` subtext
- [ ] 3.2 Renderer keeps masked links only for generated citations and unmasks all others
- [ ] 3.3 Escape markdown in quoted excerpts
- [ ] 3.4 Split long answers at paragraph or list boundaries, never inside a code block
- [ ] 3.5 Keep mentions suppressed
- [ ] 3.6 Test: a masked link reproduced from a message renders with its destination visible
- [ ] 3.7 Test: a long answer with a code block splits with whole code blocks
- [ ] 3.8 Test: "@everyone" in an answer notifies no one

## 4. Verification

- [ ] 4.1 Live: "call me Leo", then ask a question and be addressed as Leo
- [ ] 4.2 Live: set an email, ask in a channel what the assistant knows about you, and confirm the email is not shown there
- [ ] 4.3 Live: a price answer renders with bold figure and subtext source
