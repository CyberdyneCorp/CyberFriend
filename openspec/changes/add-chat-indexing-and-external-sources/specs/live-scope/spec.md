## Purpose

Apply a change to indexing scope in the running processes, without a redeploy.

## ADDED Requirements

### Requirement: Scope changes apply without a restart

The bot and ingest processes SHALL apply a change to indexing scope within a
bounded refresh period.

#### Scenario: Channel added
- WHEN a channel is added to scope
- THEN ingest SHALL begin capturing and backfilling it within the refresh period
- AND retrieval SHALL include it for people who may read it

#### Scenario: Channel removed
- WHEN a channel is removed from scope
- THEN capture SHALL stop and retrieval SHALL exclude it within the refresh
  period

### Requirement: A failed refresh keeps the current scope

When stored scope cannot be read, a process SHALL keep the scope it has rather
than falling back to the environment.

#### Scenario: Database unavailable during refresh
- WHEN a refresh cannot read stored scope
- THEN the process SHALL continue with its current scope
