"""Structured logging setup, shared by every entrypoint."""

from __future__ import annotations

import logging

import structlog

#: HTTP client libraries that log every request URL at INFO. Infura (and other
#: providers) put the API key in the URL path, so at INFO these wrote the key
#: into production logs on every chain read. WARNING keeps their errors.
QUIET_HTTP_LOGGERS = ("httpx", "httpx2", "httpcore", "httpcore2")


def configure(level: str = "INFO") -> None:
    logging.basicConfig(format="%(message)s", level=getattr(logging, level.upper()))
    for name in QUIET_HTTP_LOGGERS:
        logging.getLogger(name).setLevel(max(logging.WARNING, getattr(logging, level.upper())))
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper())
        ),
        cache_logger_on_first_use=True,
    )
