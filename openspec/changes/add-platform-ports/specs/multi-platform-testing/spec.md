## ADDED Requirements

### Requirement: End-to-end scenarios are written once and run per platform

The e2e harness SHALL expose a platform-neutral scenario API (participants,
direct messages, channel messages, commands, button presses, assertions on
replies) over pluggable platform wires. A shared scenario SHALL run on every
wire whose platform has the capabilities the scenario declares, and SHALL be
skipped, visibly, on the others.

#### Scenario: A shared scenario on a platform lacking a capability
- WHEN a scenario requires channels and runs against a wire whose platform has
  none
- THEN it SHALL be reported as skipped for that platform, not passed

#### Scenario: Scenario code names no platform type
- WHEN a shared scenario is written
- THEN it SHALL NOT import a platform SDK type such as `discord.Member`

### Requirement: The Discord suite is the acceptance gate of the refactor

Every pull request of this change SHALL pass the existing Discord end-to-end
scenarios with unchanged snapshots, driven through the generalised harness.

#### Scenario: A snapshot changes
- WHEN a pull request of this change alters any existing Discord snapshot
- THEN the change SHALL be treated as a regression unless the snapshot
  difference is a documented, intended fix in its own pull request

### Requirement: Privacy regressions are asserted on every platform

The harness SHALL run the privacy scenarios (facts about someone else,
DM-only facts in a shared place, audience-bounded evidence, the egress
identifier guard) against every enabled wire that can express them.

#### Scenario: Adding a platform
- WHEN a new platform wire is added
- THEN the privacy scenarios SHALL run against it without being rewritten
