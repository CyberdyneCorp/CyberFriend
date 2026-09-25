## ADDED Requirements

### Requirement: The user area shows only the signed-in person's own data

The user area SHALL determine the person solely from the signed-in account's
link, and SHALL NOT accept a person or platform identifier from the browser.
An account with no link, and an unknown account, SHALL receive the same
response.

#### Scenario: Signed in and linked
- WHEN a linked person opens the user area
- THEN they SHALL see their privacy dashboard and their feature requests

#### Scenario: Supplying another person's id
- WHEN a request names a person or platform id
- THEN it SHALL have no effect on whose data is returned

#### Scenario: Not linked
- WHEN a signed-in account has no link
- THEN the response SHALL say that no profile is linked and how to link one

### Requirement: A user session never grants console access

User sessions SHALL be separate from console sessions. A user session SHALL
NOT satisfy any console route, and a console session SHALL NOT satisfy any user
area route.

#### Scenario: User session on an admin route
- WHEN a request to a console route carries only a user session
- THEN it SHALL be refused as unauthenticated

### Requirement: Deleting everything from the web needs a fresh sign-in

The user area SHALL offer the same delete-everything action as the assistant.
It SHALL require a sign-in within the last five minutes, the typed confirmation
word and cross-site request protection, and SHALL end the link and the session
afterwards.

#### Scenario: Stale sign-in
- WHEN a person asks to delete everything more than five minutes after signing
  in
- THEN they SHALL be asked to sign in again first

#### Scenario: Completed
- WHEN the deletion is confirmed
- THEN the person's data SHALL be erased as for the assistant's command
- AND their account link SHALL be removed and their session ended

### Requirement: Feature requests from the web follow the same rules

Suggestions submitted in the user area SHALL be subject to the same content
refusals, deduplication and rate limit as suggestions made in chat.

#### Scenario: Suggestion with a phone number
- WHEN a person submits a suggestion containing a phone number on the web
- THEN it SHALL be refused with the reason
