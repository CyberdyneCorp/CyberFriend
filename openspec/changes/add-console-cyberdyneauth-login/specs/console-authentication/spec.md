## ADDED Requirements

### Requirement: Console sign-in goes through CyberdyneAuth without tokens reaching the browser

When an identity provider is configured, the console SHALL sign people in with
the OIDC authorization code flow with PKCE (S256), performed by the admin API.
The browser SHALL receive only an opaque, httpOnly, Secure, SameSite=Strict
session cookie, and SHALL NOT receive an access, refresh or id token.

#### Scenario: Successful sign-in
- WHEN a person completes sign-in at CyberdyneAuth and returns with a valid code
  and the state issued for that sign-in
- THEN the admin API SHALL exchange the code server-side
- AND SHALL set a session cookie and send the browser to the console

#### Scenario: State replayed or unknown
- WHEN a callback arrives with a state that was not issued, has expired or was
  already used
- THEN the sign-in SHALL be refused and no session created

#### Scenario: No identity provider configured
- WHEN no issuer is configured
- THEN the sign-in routes SHALL NOT be available
- AND bearer tokens SHALL work as before

### Requirement: Access tokens are verified locally

The system SHALL accept an access token only if its RS256 signature verifies
against the issuer's published keys, its issuer equals the one in discovery,
it has not expired, its `type` is `access`, and its audience is `cyberfriend`.
People SHALL be identified by `sub`.

#### Scenario: Token for another audience
- WHEN an access token's audience is not `cyberfriend`
- THEN the system SHALL treat the session as unauthenticated

#### Scenario: Token of another type
- WHEN a token's `type` is anything other than `access`
- THEN the system SHALL NOT accept it

#### Scenario: Signing key rotated
- WHEN a token is signed with a key id not in the cached key set
- THEN the system SHALL refetch the key set once and verify against it

### Requirement: Roles come from the verified token and absence denies

The system SHALL grant the admin role for `<client_id>:admin` and the operator
role for `<client_id>:operator`, taken only from the verified access token.
Admin SHALL imply operator. An absent roles claim, or one without either role,
SHALL grant no console access.

#### Scenario: No roles claim
- WHEN a verified token carries no roles claim
- THEN the person SHALL NOT be given a console session
- AND the response SHALL NOT reveal anything beyond "no access"

#### Scenario: Role withdrawn
- WHEN a person's admin role is removed at the identity provider
- THEN their session SHALL lose admin rights no later than their next token
  refresh

### Requirement: Read-only operators cannot change anything

Every console route SHALL declare the minimum role it needs, and any route that
is not a read SHALL require admin. A request below the required role SHALL be
refused with a distinct "forbidden" response.

#### Scenario: Operator attempts a change
- WHEN a person with the operator role sends a request that changes
  configuration, opt-outs, tokens, federation or channels
- THEN the system SHALL refuse it as forbidden and change nothing

#### Scenario: Route without a declared role
- WHEN a route exists that the role table does not name
- THEN requests to it SHALL be refused

### Requirement: Cookie sessions are protected against cross-site use

A request authenticated by the session cookie that is not a read SHALL carry the
console's custom request header, and SHALL come from the console's own origin
when an Origin is sent. Console responses SHALL forbid framing by other sites.

#### Scenario: Cross-site form post
- WHEN another site causes the browser to send a state-changing request with the
  session cookie but without the custom header
- THEN the system SHALL refuse it

#### Scenario: Framing
- WHEN another site tries to embed the console in a frame
- THEN the response headers SHALL forbid it

### Requirement: Sessions refresh safely and end cleanly

The system SHALL refresh an expiring access token server-side, with at most one
refresh in flight per session. A failed refresh SHALL end the session. Signing
out SHALL revoke the session before sending the person to the provider's
end-session endpoint with the id token hint.

#### Scenario: Concurrent requests near expiry
- WHEN two requests on one session arrive as its access token is about to expire
- THEN exactly one refresh SHALL be sent to the provider
- AND both requests SHALL proceed with the new token

#### Scenario: Sign out
- WHEN a person signs out
- THEN the session cookie SHALL stop working immediately
- AND the browser SHALL be sent to the provider's end-session endpoint

### Requirement: Exactly one credential per request

A request SHALL carry either one bearer token or one session cookie. A request
carrying both SHALL be refused as unauthenticated.

#### Scenario: Script token plus browser cookie
- WHEN a request carries a bearer header and a session cookie
- THEN the system SHALL refuse it without choosing either

### Requirement: Static operator tokens remain for scripts

Operator tokens issued from the shell SHALL keep working and SHALL act with the
admin role, attributed to the operator they name.

#### Scenario: Script uses a token
- WHEN a script calls the admin API with a valid operator token
- THEN it SHALL be served as admin and its changes recorded against that
  operator

### Requirement: Changes made through sign-in are attributed to the person

A change made through a CyberdyneAuth session SHALL be recorded against the
person's subject, with their email alongside for display. The change record
SHALL remain append-only.

#### Scenario: Admin changes a setting after signing in
- WHEN a signed-in admin changes a setting
- THEN the record SHALL name `oidc:<sub>` as the actor and carry their email
