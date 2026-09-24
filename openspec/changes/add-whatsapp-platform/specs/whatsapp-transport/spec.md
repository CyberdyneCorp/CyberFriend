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
with the app secret (compared in constant time), SHALL respond 200 before any
model or network work, and SHALL process each message id (`wamid`) at most
once regardless of retries or delivery order.

#### Scenario: A forged webhook
- WHEN a POST arrives with a missing or wrong signature
- THEN it SHALL be rejected with 401 and nothing in it SHALL be processed

#### Scenario: A retried delivery
- WHEN Meta delivers the same `wamid` twice
- THEN the person SHALL get one reply

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

#### Scenario: No template for the language
- WHEN the window is closed and no template exists for the person's language
- THEN the template in the deployment's default language SHALL be sent if
  registered, otherwise nothing SHALL be sent and the failure recorded

### Requirement: A templated message leads to the full content on request

A proactive template SHALL carry a quick-reply button; when the person taps
it, the full content SHALL be sent free-form in the window that tap opened.
Held content SHALL expire after seven days.

#### Scenario: Tapping "Show"
- WHEN a person taps "Show" on a scheduled-answer template
- THEN the full scheduled answer SHALL be sent as free-form messages

#### Scenario: Tapping after expiry
- WHEN a person taps a template's button more than seven days after it was
  sent
- THEN the reply SHALL say the content has expired and offer to run it again

### Requirement: Proactive messages stay within limits and cost controls

The adapter SHALL stop sending proactive templates when the configured daily
cap is reached, prioritising alerts over digests, SHALL batch obligation
notifications into at most one daily digest per person, and SHALL record each
template delivery's category and cost for the admin console and traces.

#### Scenario: The daily cap is reached
- WHEN `WHATSAPP_DAILY_PROACTIVE_CAP` templates have been sent today
- THEN further digests SHALL be deferred to the next day
- AND alerts SHALL be deferred only after digests

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
