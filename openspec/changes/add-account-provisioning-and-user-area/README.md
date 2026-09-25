# add-account-provisioning-and-user-area

A person who confirms, in a DM, the exact name and email to send gets a
CyberdyneAuth account provisioned through CyberdyneAuth's approved
`POST /api/v1/users/provision` contract. CyberdyneAuth creates an unverified,
passwordless account and emails an invitation; only whoever controls that
inbox can accept it. After accepting, the person links the account to their
Discord identity with a single-use code and signs in to a web user area with
their privacy dashboard, "delete everything" and their feature requests.

- `proposal.md`: why, and what changes
- `design.md`: consent, the provisioning contract, limits, linking a subject to a person, the user session
- `specs/account-provisioning/spec.md`: consent, provisioning and linking
- `specs/user-area/spec.md`: the web user area
- `tasks.md`: progress, with blocked items marked
