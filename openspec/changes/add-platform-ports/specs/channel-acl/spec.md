## MODIFIED Requirements

### Requirement: Viewer channel visibility resolution

The system SHALL determine, for a given person, the set of indexed channels
that person is permitted to read on the channel's source platform, honouring
that platform's own access rules: on Discord the server's role permissions
and per-channel permission overwrites; on Slack public/private membership,
guest and Slack Connect rules (`slack-access-control`); on a platform without
channels, no channel at all unless the person is linked to an identity that
has some (`platform-identity`).

#### Scenario: Member of a channel they can read
- GIVEN a person whose platform access rules let them view a channel and read
  its history
- WHEN their visible channel set is resolved
- THEN that channel SHALL be included

#### Scenario: Channel the person cannot view
- GIVEN a person whose platform access rules do not let them view a channel
- WHEN their visible channel set is resolved
- THEN that channel SHALL NOT be included

#### Scenario: Channel viewable without history access
- GIVEN a Discord person who may view a channel but whose permissions deny
  reading its history
- WHEN their visible channel set is resolved
- THEN that channel SHALL NOT be included

#### Scenario: Person who has left the server or workspace
- GIVEN a person who is no longer a member of the Discord server or Slack
  workspace, or whose account is deactivated
- WHEN their visible channel set is resolved
- THEN the set SHALL be empty

#### Scenario: A channel on another platform
- GIVEN a channel on a platform other than the person's own, and no link
  between the person and an identity on that platform
- WHEN their visible channel set is resolved
- THEN that channel SHALL NOT be included
