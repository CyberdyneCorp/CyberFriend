## ADDED Requirements

### Requirement: An account is requested only after explicit consent to exact values

The assistant SHALL request an identity-provider account for a person only
after showing them, in a direct message, the exact name and email that will be
sent, and receiving their confirmation. It SHALL send no other personal data
than the name, email and language.

#### Scenario: Confirming
- WHEN a person reviews the name and email and presses Confirm
- THEN the assistant SHALL record their consent and request the account

#### Scenario: Asked in a server channel
- WHEN a person asks for an account in a server channel
- THEN the assistant SHALL continue only in a direct message

#### Scenario: Other facts are never sent
- WHEN an account is requested for a person who has a phone, address or wallet
  on file
- THEN only the name, email and language SHALL be sent

### Requirement: The consent states what outlives deletion

Before the person confirms, the assistant SHALL state that the identity
provider will email an invitation to the address and the account works only
once it is accepted, and that the identity-provider account, with that name
and email, is not deleted by the assistant's delete-everything action until
the provider offers deletion, and how to request its deletion.

#### Scenario: Reading the consent
- WHEN the consent message is shown
- THEN it SHALL include both statements in the person's language

### Requirement: Provisioning follows the provider's contract and cannot reveal an account

The system SHALL request accounts only through the provider's provisioning
endpoint, authenticated as a client limited to provisioning, with the email and
optionally the name and language. The provider creates an unverified account
without a password and invites the email's owner. The assistant's reply SHALL
be the same in every outcome.

#### Scenario: Account already exists
- WHEN a person requests an account for an email that already has one
- THEN the reply SHALL be identical to the reply for a new account

#### Scenario: Someone else's email
- WHEN a person requests an account for an email they do not control
- THEN no one SHALL gain access through it unless the email's owner accepts the
  invitation

#### Scenario: Provider rate limit
- WHEN the provider refuses the request because of its rate limit
- THEN the assistant SHALL tell the person to try again later
- AND SHALL NOT count it against the person's own limit

### Requirement: Provisioning is limited per person

The system SHALL allow each person at most one provisioning request in any 24
hours and at most three in any 30 days, and SHALL store the email only as a
keyed hash.

#### Scenario: Second request within a day
- WHEN a person asks for an account again within 24 hours of a request
- THEN the assistant SHALL refuse it and say when they can try again
- AND nothing SHALL be sent to the provider

#### Scenario: Fourth request in 30 days
- WHEN a person has made three requests in the last 30 days
- THEN a further request SHALL be refused and nothing sent

#### Scenario: Stored email reference
- WHEN consent or a request is recorded
- THEN the email SHALL be stored only as a hash keyed with a server secret

### Requirement: A web account is linked to a person only by proof

The system SHALL link an identity-provider account to a person only when the
person follows a single-use link, valid for 15 minutes, from their direct
message, signs in in the same browser, the provider reports a verified email
equal to the consented email, and the userinfo subject equals the token
subject. The person SHALL be told when a link is made and SHALL be able to
undo it.

#### Scenario: Link code forwarded to someone else
- WHEN someone other than the person uses the link and signs in with a
  different email
- THEN no link SHALL be made

#### Scenario: Unverified email
- WHEN the provider reports the email as unverified
- THEN no link SHALL be made

#### Scenario: Subject mismatch
- WHEN the userinfo subject differs from the token subject
- THEN no link SHALL be made

#### Scenario: Link made
- WHEN a link is made
- THEN the person SHALL receive a direct message naming the masked email with an
  option to unlink

### Requirement: Provisioning is off until enabled

Account provisioning SHALL be disabled unless explicitly enabled, and SHALL
only call the identity provider through the provisioning port.

#### Scenario: Not enabled
- WHEN provisioning is not enabled
- THEN the account command SHALL NOT be offered
