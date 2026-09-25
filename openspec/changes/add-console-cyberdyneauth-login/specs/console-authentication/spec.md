## ADDED Requirements

### Requirement: Console sign-in goes through CyberdyneAuth without tokens reaching the browser

When an identity provider is configured, the console SHALL sign people in with
the OIDC authorization code flow with PKCE (S256), performed by the admin API.
The browser SHALL receive only an opaque, httpOnly, Secure, SameSite=Strict
session cookie, and SHALL NOT receive an access or refresh token. It SHALL
receive the id token only as the id token hint of the end-session URL at
sign-out, after the session is revoked.

#### Scenario: Successful sign-in
- WHEN a person completes sign-in at CyberdyneAuth and returns with a valid code
  and the state issued for that sign-in
- THEN the admin API SHALL exchange the code server-side
- AND SHALL set a session cookie and send the browser to the console

#### Scenario: State replayed or unknown
- WHEN a callback arrives with a state that was not issued, has expired or was
  already used
- THEN the sign-in SHALL be refused and no session created

#### Scenario: Callback completed in another browser
- WHEN a callback arrives without the pre-sign-in cookie set by the browser
  that started the sign-in, or with a different value
- THEN the sign-in SHALL be refused and no session created

#### Scenario: Id token nonce does not match
- WHEN the id token's nonce is missing or differs from the one issued for that
  sign-in
- THEN the sign-in SHALL be refused

#### Scenario: Subjects disagree
- WHEN the subject in the userinfo response differs from the subject of the id
  token or the access token
- THEN the sign-in SHALL be refused and the userinfo email SHALL NOT be used

#### Scenario: No identity provider configured
- WHEN no issuer is configured, whether or not the other sign-in settings are
- THEN the service SHALL start
- AND the sign-in routes SHALL NOT be available
- AND bearer tokens SHALL work as before

### Requirement: Access tokens are verified locally

The system SHALL accept an access token only if its RS256 signature verifies
against the issuer's published keys, its issuer equals the one in discovery,
it has not expired, its `type` is `access`, and its audience is `cyberfriend`.
The id token SHALL be verified by signature, issuer, audience equal to the
client id, expiry and nonce. People SHALL be identified by `sub`, and the
userinfo subject SHALL equal the token subject.

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

#### Scenario: Refreshed token without a roles claim
- WHEN a refreshed access token carries no roles claim
- THEN the session SHALL be ended and the request refused as unauthenticated

#### Scenario: Refreshed token with neither console role
- WHEN a refreshed access token carries a roles claim with neither console role
- THEN the session SHALL be ended and the request refused as forbidden

### Requirement: Read-only operators cannot change anything

Every mounted route SHALL have an explicit entry in one route-to-role table,
with no default by request method, and any route that is not a read SHALL
require admin. A request below the required role SHALL be refused with a
distinct "forbidden" response.

#### Scenario: Operator attempts a change
- WHEN a person with the operator role sends a request that changes
  configuration, opt-outs, tokens, federation or channels
- THEN the system SHALL refuse it as forbidden and change nothing

#### Scenario: Route without a declared role
- WHEN a route exists that the role table does not name, whatever its method
- THEN requests to it SHALL be refused before its handler runs
- AND the test suite, which enumerates every mounted route, SHALL fail

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

### Requirement: A bearer request ignores cookies

A request carrying a bearer header SHALL be authenticated by that bearer
alone. Cookies on the same request SHALL be ignored: the session SHALL NOT be
read, refreshed or used as a fallback.

#### Scenario: Break-glass token in a browser that holds a session cookie
- WHEN a request carries a valid bearer header and a session cookie
- THEN it SHALL be served with the bearer's role
- AND the session SHALL be left untouched

#### Scenario: Invalid bearer with a valid cookie
- WHEN a request carries an invalid bearer header and a valid session cookie
- THEN it SHALL be refused as unauthenticated

### Requirement: Static operator tokens remain for scripts, downscoped

Operator tokens issued from the shell SHALL keep working, attributed to the
operator they name. While no identity provider is configured they SHALL act as
admin. Once one is configured they SHALL act as operator only, and admin rights
SHALL come only from the identity provider's role. A token SHALL never read
personal content such as question text.

#### Scenario: Script uses a token before sign-in is configured
- WHEN no identity provider is configured and a script calls the admin API with
  a valid operator token
- THEN it SHALL be served as admin and its changes recorded against that
  operator

#### Scenario: Script uses a token after sign-in is configured
- WHEN an identity provider is configured and a script sends a change with a
  valid operator token
- THEN the system SHALL refuse it as forbidden

#### Scenario: Script token requests question text
- WHEN a request authenticated by an operator token asks for a person's
  question text
- THEN the system SHALL refuse it as forbidden, whatever the token's role

### Requirement: Changes made through sign-in are attributed to the person

A change made through a CyberdyneAuth session SHALL be recorded against the
person's subject, with their email alongside for display. The change record
SHALL remain append-only.

#### Scenario: Admin changes a setting after signing in
- WHEN a signed-in admin changes a setting
- THEN the record SHALL name `oidc:<sub>` as the actor and carry their email
