## ADDED Requirements

### Requirement: Decisions are extracted with asks

The system SHALL read each extraction candidate for the decision it concludes
in the same model call that reads it for asks, and SHALL record a decision
only when the analysed message itself settles a course of action for the
group.

#### Scenario: A conclusion in Portuguese
- WHEN a message such as "fechou, deploy na sexta então" settles a choice
- THEN the system SHALL record a decision attributed to that message and its
  author, dated at the message's time, with a summary in Portuguese

#### Scenario: A proposal
- WHEN a message proposes, asks about or leans towards a choice without
  settling it, such as "bora fazer o deploy na sexta?"
- THEN the system SHALL NOT record a decision

#### Scenario: A status report using a decision word
- WHEN a message reports that something finished, such as "fechou a sprint"
- THEN the system SHALL NOT record a decision

#### Scenario: One array missing from the model's reply
- WHEN the model's reply lacks the decisions array or the asks array
- THEN the system SHALL treat the missing array as empty and keep the other

### Requirement: Decision markers make a message a candidate

The system SHALL treat a message carrying a decision marker, in English or
Brazilian Portuguese, as an extraction candidate even when it mentions no one,
replies to no one and carries no other signal.

#### Scenario: Brazilian phrasing
- WHEN a message says "fechou", "fechado", "bora", "combinado", "ficou
  decidido", "tá decidido", "vamos com" or "vamos seguir com"
- THEN the message SHALL be sent to the extraction model

#### Scenario: English phrasing
- WHEN a message says "we decided", "let's go with", "agreed" or "final call"
- THEN the message SHALL be sent to the extraction model

#### Scenario: A marker word used as something else
- WHEN a message uses a form that mostly describes something else, such as
  "a loja tá fechada", "vamos de carro" or "going with my family", and
  carries no other signal
- THEN the message SHALL NOT be sent to the extraction model

#### Scenario: No marker and no other signal
- WHEN a message carries neither a decision marker nor an ask signal
- THEN the message SHALL NOT be sent to the extraction model

### Requirement: Decisions are stored stably and withdrawn on re-extraction

The system SHALL identify a decision by its source message and topic, never
by its generated summary, and SHALL replace the decisions of a message each
time that message is extracted.

#### Scenario: Extracted twice
- WHEN the same message is extracted again and the same topic is found
- THEN the system SHALL update the existing decision rather than add another

#### Scenario: An edit takes the decision back
- WHEN a message is edited so that re-extraction finds no decision in it
- THEN the system SHALL remove the decision recorded from it

#### Scenario: An edit re-read out of the corpus
- WHEN the backlog pass re-extracts an edited message on its own
- THEN the model SHALL be shown the messages before it in its channel and its
  reply parent, as the live pass would have shown them
- AND a decision that still stands with that conversation in view SHALL be
  kept, with the same evidence

#### Scenario: An edit removes every signal
- WHEN a message is edited so that it is no longer an extraction candidate
- THEN the system SHALL remove any decision recorded from it

#### Scenario: Storing a decision fails
- WHEN the decision cannot be stored
- THEN the asks extracted from the same message SHALL still be stored

### Requirement: Every message a decision rests on is recorded

The system SHALL record, with each decision, the source message and every
message shown to the model alongside it.

#### Scenario: A decision settling an earlier proposal
- WHEN a decision is extracted from a message whose context includes the
  proposal it settles
- THEN the proposal SHALL be recorded as evidence of that decision

### Requirement: Decisions are embedded when stored

The system SHALL embed each decision's topic and summary when storing it, and
SHALL store the decision without an embedding when embedding fails.

#### Scenario: The embedding endpoint fails
- WHEN the embedding call fails while a decision is stored
- THEN the decision SHALL be stored with no embedding
- AND an embedding stored earlier for it SHALL be kept

### Requirement: Decisions leave with the content they came from

The system SHALL remove a decision when its source message or any message it
rests on is deleted, when
retention purges it or any message it rests on, and when a person who stated
it or wrote any message it rests on opts out.

#### Scenario: The source message is deleted
- WHEN a person deletes the source message, which tombstones it
- THEN its decisions SHALL be removed
- AND when the message row itself is deleted, its decisions SHALL be deleted
  with it

#### Scenario: A message a decision rests on is deleted
- WHEN a person deletes a message recorded as evidence of a decision, such as
  the proposal it settled
- THEN that decision SHALL be removed

#### Scenario: Retention
- WHEN retention runs with a cutoff
- THEN decisions dated before the cutoff SHALL be removed
- AND decisions resting on any message dated before the cutoff SHALL be removed
- AND the retention report SHALL count them

#### Scenario: Opting out of a proposal
- WHEN a person opts out and a decision stated by someone else rests on a
  proposal that person wrote
- THEN that decision SHALL be removed
- AND the opt-out report SHALL count it

### Requirement: Decision questions are answered from the decision log

The system SHALL answer a question about what a group decided -- in
Portuguese ("o que decidimos sobre Y?", "o que ficou decidido sobre Y?",
"qual foi a decisão sobre Y?") or English ("what did we decide about Y?",
"what was decided about Y?") -- from stored decisions, recognised without a
model call, rendered without a chat-model call, in the language the question
was asked in, listing each decision with its local date, newest first, and a
citation of the message that settled it.

#### Scenario: A Portuguese question
- WHEN a person asks "o que decidimos sobre o deploy?" and a decision about
  the deploy is stored in a channel they and the room can read
- THEN the reply SHALL be in Portuguese, list that decision dated in
  `ANSWER_TIMEZONE`, and cite the message that settled it with a link to it
- AND no chat model SHALL be called; only the topic SHALL be embedded

#### Scenario: An English question
- WHEN a person asks "what did we decide about the deploy?"
- THEN the reply SHALL be in English, with the decision's summary in the
  language it was decided in

#### Scenario: A period
- WHEN the question names a time ("semana passada", "yesterday")
- THEN only decisions taken in that span, read as calendar days in
  `ANSWER_TIMEZONE`, SHALL be listed, and the heading SHALL state the span
- AND a question with neither topic nor time SHALL list the last 30 days and
  say so

#### Scenario: Not a lookup of the log
- WHEN the question asks the bot what it decided ("what did you decide", or
  the subjectless "o que decidiu?"), asks about one person's decision, asks
  for help choosing ("decide between A and B"), names its topic only by a
  pronoun ("sobre isso", "about me") or a channel ("no #leadership"), asks
  more than one thing, or is a market, fact, catch-up, said-by or obligation
  question
- THEN the decision log SHALL NOT answer it

#### Scenario: A follow-up with no topic
- WHEN the question names no topic and opens as a continuation ("e o que
  decidimos?", "and what did we decide?")
- THEN the decision log SHALL NOT answer it, and retrieval, which has the
  conversation, SHALL

### Requirement: A decision answer never discloses what the room cannot read

The system SHALL list a decision only when its channel, and the channel of
every message it rests on, are readable by both the asker and everyone the
reply reaches, and only while its source and every message it rests on are
alive.

#### Scenario: A private decision asked about in a public channel
- WHEN a decision was taken in #leadership and a lead asks about it in
  #general, which not everyone there can read #leadership from
- THEN the decision SHALL NOT be listed and the question SHALL be answered by
  ordinary retrieval under the same scope
- AND the same question asked by the lead in a direct message SHALL list it

#### Scenario: A deleted conclusion or proposal
- WHEN the message that settled a decision, or a message it rests on such as
  the proposal it settled, is deleted
- THEN the decision SHALL NOT be listed, even before its row is withdrawn

#### Scenario: Evidence from an unreadable channel
- WHEN a decision rests on a message in a channel the viewer cannot read
- THEN the decision SHALL NOT be listed

### Requirement: No match falls through to retrieval

The system SHALL answer a decision question by ordinary retrieval, never by a
statement that nothing was decided, when no readable decision is similar
enough to the question's topic.

#### Scenario: Below the similarity floor
- WHEN no stored decision reaches `DECISION_MIN_SIMILARITY` against the
  topic, and none stored without an embedding carries the topic's words
- THEN the question SHALL be answered by ordinary retrieval

#### Scenario: The topic cannot be embedded
- WHEN embedding the topic fails
- THEN the question SHALL be answered by ordinary retrieval

### Requirement: History extracted before the decision log can be backfilled

The system SHALL provide an operator command (`just decisions-backfill
--since YYYY-MM-DD`) that marks pending for extraction only the messages that
are live, created on or after the given day (midnight UTC), in a channel in
indexing scope, and matched by the candidate filter's own `DECISION_MARKERS`
pattern; it SHALL print how many it reset and extract nothing itself, leaving
the reset messages to the existing backlog worker and its rate bound. The day
SHALL reach the database only as a bound parameter.

#### Scenario: Only marker-bearing messages inside the window are reset
- WHEN the command runs over history that holds, besides marker-bearing live
  messages in scope inside the window, a message without a marker, one
  before the window, one in a channel outside scope and a deleted one
- THEN only the marker-bearing live messages in scope inside the window
  SHALL become pending
- AND a second run SHALL reset nothing still pending

#### Scenario: Stored scope cannot be read
- WHEN stored configuration cannot be read
- THEN the command SHALL stop without resetting anything

#### Scenario: Re-extraction keeps the asks already recorded
- WHEN the backlog worker re-extracts a reset message that carries asks
- THEN every re-found ask SHALL keep its key and status
- AND a corrected ask SHALL be kept, with its correction, even if the new
  pass does not find it

#### Scenario: Old history becomes answerable
- WHEN history captured and extracted before decisions existed is backfilled
  and the backlog worker drains it
- THEN a decision question about it SHALL get a dated answer citing the
  message that settled it
