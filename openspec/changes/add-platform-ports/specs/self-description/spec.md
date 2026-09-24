## MODIFIED Requirements

### Requirement: Every command is listed

The reply SHALL name each command the assistant offers on the asker's
platform and conversation kind, and what it does.

#### Scenario: Asked what it can do
- WHEN someone asks what the assistant can do or which commands exist
- THEN every command available on their platform and conversation kind SHALL
  be listed

#### Scenario: A command that is not available
- WHEN a command's feature is switched off for this deployment, or requires a
  capability the asker's platform lacks
- THEN that command SHALL NOT be listed

#### Scenario: Asked in a direct message
- WHEN someone asks what the assistant can do in a direct message
- THEN a command the platform does not offer in a direct message (on Discord,
  one registered only on the server) SHALL NOT be listed
