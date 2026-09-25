## MODIFIED Requirements

### Requirement: Operator identity comes from the credential

Every request SHALL be authenticated, and the credential SHALL identify who is
acting and with which role. The credential SHALL be either an operator token
issued to a named operator or a CyberdyneAuth session for a person identified
by subject. A credential that grants access without naming who holds it SHALL
NOT be accepted.

#### Scenario: Request with a valid credential
- WHEN a request arrives with an operator token or a valid session
- THEN the system SHALL act as that operator or person, with the role the
  credential grants
- AND SHALL record them against anything the request changes

#### Scenario: Request without a credential
- WHEN a request arrives with no credential, or one that is unknown, malformed,
  expired or revoked
- THEN the system SHALL refuse it
- AND the refusal SHALL be indistinguishable between those cases

#### Scenario: Console served under a path prefix
- WHEN the console is served behind a path prefix, so that the request path
  carries the prefix and the router resolves routes without it
- THEN any request the router dispatches to a console route SHALL be
  authenticated first
- AND the prefix SHALL NOT make any route reachable without a credential

#### Scenario: Operator named in the request
- WHEN a request states which operator or role it is acting as
- THEN that SHALL have no effect; only the credential decides

#### Scenario: Revoking one operator
- WHEN an operator's token or a person's session is revoked
- THEN their subsequent requests SHALL be refused
- AND every other credential SHALL continue to work
