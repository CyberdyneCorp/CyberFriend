# add-account-provisioning-and-user-area

A person who gives the bot their name and email and presses Confirm gets a
CyberdyneAuth account created for them. They can then sign in to a web user
area with their privacy dashboard, "delete everything" and their feature
requests. The provisioning call depends on a CyberdyneAuth endpoint that
doesn't exist yet. Our side is designed against an `AccountProvisioner` port,
and the external contract is an open dependency.

- `proposal.md`: why, and what changes
- `design.md`: consent, the port, linking a CyberdyneAuth subject to a person, the user session
- `specs/account-provisioning/spec.md`: consent and provisioning
- `specs/user-area/spec.md`: the web user area
- `tasks.md`: progress, with blocked items marked
