"""Failures that are not answers.

A configuration error is raised at load time, never mid-request: the whole
point of a closed condition vocabulary and of boot-time capability checks is
that a bad deployment fails before it serves anyone.

`RetrievalUnavailable` is deliberately distinct from "found nothing". A
dependency failure rendered as an empty result is how a system reports that
nobody said anything when in truth it could not look.
"""

from __future__ import annotations


class ConfigurationError(Exception):
    """Raised at load time by a configuration that cannot be served safely."""


class RetrievalUnavailable(Exception):
    """The corpus could not be consulted. Never a synonym for an empty result."""
