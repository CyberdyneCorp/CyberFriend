## ADDED Requirements

### Requirement: An account is created only after explicit consent to exact values

The assistant SHALL create an identity-provider account for a person only after
showing them, in a direct message, the exact email and name that will be sent,
and receiving their confirmation. It SHALL send no other personal data.

#### Scenario: Confirming
- WHEN a person reviews the email and name and presses Confirm
- THEN the assistant SHALL record their consent and request the account

#### Scenario: Asked in a server channel
- WHEN a person asks for an account in a server channel
- THEN the assistant SHALL continue only in a direct message

#### Scenario: Other facts are never sent
- WHEN an account is requested for a person who has a phone, address or wallet
  on file
- THEN only the email, name and an opaque reference SHALL be sent

### Requirement: Replies do not reveal whether an account already existed

The assistant's reply, and anything the person can observe, SHALL be the same
whether an account was created or already existed.

#### Scenario: Account already exists
- WHEN a person requests an account for an email that already has one
- THEN the reply SHALL be identical to the reply for a new account

### Requirement: A web account is linked to a person only by proof

The system SHALL link an identity-provider account to a person only when the
person follows a single-use link, valid for 15 minutes, from their direct
message, signs in, and the provider reports a verified email equal to the
consented email. The person SHALL be told when a link is made and SHALL be
able to undo it.

#### Scenario: Link code forwarded to someone else
- WHEN someone other than the person uses the link and signs in with a
  different email
- THEN no link SHALL be made

#### Scenario: Unverified email
- WHEN the provider reports the email as unverified
- THEN no link SHALL be made

#### Scenario: Link made
- WHEN a link is made
- THEN the person SHALL receive a direct message naming the masked email with an
  option to unlink

### Requirement: Provisioning is off until the provider contract exists

Account provisioning SHALL be disabled unless explicitly enabled, and SHALL
only call the identity provider through the provisioning port.

#### Scenario: Not enabled
- WHEN provisioning is not enabled
- THEN the account command SHALL NOT be offered
