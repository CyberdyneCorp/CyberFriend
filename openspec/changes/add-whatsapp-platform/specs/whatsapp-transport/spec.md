## ADDED Requirements

### Requirement: WhatsApp is reached only through the official Cloud API

The WhatsApp adapter SHALL use only Meta's WhatsApp Business Cloud API,
directly or through a business solution provider reselling it. It SHALL NOT
use the retired On-Premises API or any unofficial client that automates a
personal WhatsApp account.

#### Scenario: Configuration names an unofficial client
- WHEN a deployment configures a transport other than the Cloud API or a
  supported provider
- THEN the process SHALL refuse to start

### Requirement: Webhooks are authenticated, acknowledged fast and processed once

The webhook endpoint SHALL answer Meta's verification GET only when
`hub.verify_token` equals the configured verify token, SHALL accept a POST
only when `X-Hub-Signature-256` matches an HMAC-SHA256 of the raw body keyed
with the app secret (compared in constant time), SHALL ignore a payload whose
`metadata.phone_number_id` is not the configured number, SHALL record each
message id (`wamid`) durably with only its timestamps before responding 200
and before any model or network work, and SHALL process each recorded message
exactly once regardless of retries, delivery order or a process restart.

#### Scenario: A forged webhook
- WHEN a POST arrives with a missing or wrong signature
- THEN it SHALL be rejected with 401 and nothing in it SHALL be processed

#### Scenario: A retried delivery
- WHEN Meta delivers the same `wamid` twice
- THEN the person SHALL get one reply

#### Scenario: A restart after the insert
- WHEN the process stops after recording a message and before answering it
- THEN after restart the message SHALL be answered once

#### Scenario: A payload for another number
- WHEN a signed payload names a `phone_number_id` other than
  `WHATSAPP_PHONE_NUMBER_ID`
- THEN it SHALL be acknowledged and nothing in it SHALL be processed

#### Scenario: What the dedup row holds
- WHEN a message has been processed
- THEN its inbound row SHALL hold only the `wamid` and timestamps, and no
  message body

#### Scenario: Verification with a wrong token
- WHEN a verification GET carries a verify token that does not match
- THEN it SHALL be answered 403 without echoing the challenge

#### Scenario: Out-of-order delivery
- WHEN a later message arrives before an earlier one
- THEN each reply SHALL quote the message it answers

### Requirement: Free-form messages are sent only inside the 24-hour window

The adapter SHALL record the time of each person's last inbound message and
SHALL send free-form text, interactive or media messages only while that
window (24 hours, with a safety margin) is open. Outside it, it SHALL send
only an approved template registered for the message's purpose and the
person's language, and SHALL report `NEEDS_TEMPLATE` when none is registered.

#### Scenario: An answer to a question
- WHEN a person asks a question and the answer is ready within the window
- THEN the answer SHALL be sent free-form

#### Scenario: A message after the window
- WHEN a proactive message is due 30 hours after the person last wrote
- THEN only the registered utility template for that purpose and language
  SHALL be sent

#### Scenario: An alert fires for a BSUID-only person
- WHEN an opted-in person known only by BSUID, with no phone number, has an
  alert fire outside the window
- THEN the template SHALL be delivered to their BSUID recipient address

#### Scenario: No template for the language
- WHEN the window is closed and no template exists for the person's language
- THEN the template in the deployment's default language SHALL be sent if
  registered, otherwise nothing SHALL be sent and the failure recorded

### Requirement: A templated message leads to the full content on request

A proactive template SHALL carry a quick-reply button; when the person taps
it, the full content SHALL be sent free-form in the window that tap opened.
Held content SHALL expire after seven days.

#### Scenario: Tapping "Show"
- WHEN a person taps "Show" ("Ver" in Portuguese) on a scheduled-answer
  template
- THEN the full scheduled answer SHALL be sent as free-form messages

#### Scenario: Tapping after expiry
- WHEN a person taps a template's button more than seven days after it was
  sent
- THEN the reply SHALL say the content has expired and offer to run it again

### Requirement: Proactive messages stay within limits and cost controls

The adapter SHALL stop sending proactive templates to new recipients when the
number of unique recipients of business-initiated templates over the rolling
last 24 hours reaches the configured cap, prioritising alerts over digests,
SHALL batch obligation notifications into at most one daily digest per
person, and SHALL record each template delivery's category, cost and currency
for the admin console and traces.

#### Scenario: The cap is reached
- WHEN templates have reached `WHATSAPP_DAILY_PROACTIVE_CAP` unique
  recipients in the last 24 hours
- THEN further digests to new recipients SHALL be deferred until the rolling
  count falls below the cap
- AND alerts SHALL be deferred only after digests

#### Scenario: Cost recorded with currency
- WHEN a template is delivered to a Brazilian number after 2026-07-01
- THEN its cost SHALL be recorded with currency `BRL`

#### Scenario: Several asks in a day
- WHEN five asks addressed to a linked person are extracted in one day
- THEN at most one digest template SHALL be sent to them on WhatsApp that day

### Requirement: Messages fit WhatsApp's format and limits

Replies SHALL be rendered in WhatsApp markup (`*bold*`, `_italic_`,
`~strike~`, monospace blocks), with no headings, tables or masked links,
split into messages of at most 4,096 characters, with URLs shown in full.
Reply buttons SHALL be at most three with labels of at most 20 characters,
and list messages at most ten rows with titles of at most 24 characters.

#### Scenario: A portfolio table
- WHEN a portfolio answer contains a table
- THEN it SHALL be sent as a list with every value preserved

#### Scenario: A long answer
- WHEN an answer is 9,000 characters
- THEN it SHALL be sent as three messages, none over 4,096 characters, with
  no code block broken

### Requirement: Media is fetched immediately with the bearer token

Inbound media SHALL be resolved and downloaded with the access token as soon
as its webhook is processed, within the media URL's five-minute lifetime, and
SHALL be deleted once processed unless it is a document the person asked to
keep.

#### Scenario: An expired media URL
- WHEN a download fails because the URL expired
- THEN the media id SHALL be resolved again once and the download retried

### Requirement: Template status changes are followed

The adapter SHALL subscribe to template status and category webhooks. A
template that Meta pauses, rejects or disables, or recategorises away from
utility, SHALL be disabled in the registry at once; sends for its purpose
SHALL then hold the content until the person's next inbound message, with
the outcome recorded as `deferred`, and the operator SHALL be alerted.

#### Scenario: A template is paused
- WHEN `message_template_status_update` reports `cf_alert_fired` as paused
- THEN no further send SHALL use it
- AND an alert firing outside the window SHALL be held until the person next
  writes, and the operator alerted

#### Scenario: A template is moved to marketing
- WHEN `template_category_update` moves `cf_scheduled_ready` to marketing
- THEN it SHALL be disabled and never sent as a marketing message
