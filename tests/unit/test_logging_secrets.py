"""Regression: request URLs, which carry provider API keys, stay out of the logs.

Production logs held `POST https://arbitrum-mainnet.infura.io/v3/<key>` on every
chain read, written by httpx's INFO request log.
"""

from __future__ import annotations

import logging

import pytest

from chatmemory import logging as log_setup


@pytest.mark.parametrize("name", log_setup.QUIET_HTTP_LOGGERS)
def test_http_client_request_lines_are_not_logged_at_info(name: str) -> None:
    log_setup.configure("INFO")
    assert not logging.getLogger(name).isEnabledFor(logging.INFO)
    assert logging.getLogger(name).isEnabledFor(logging.WARNING)


def test_an_infura_request_line_never_reaches_the_log(caplog: pytest.LogCaptureFixture) -> None:
    log_setup.configure("INFO")
    with caplog.at_level(logging.INFO):
        logging.getLogger("httpx").info(
            'HTTP Request: POST https://base-mainnet.infura.io/v3/%s "HTTP/1.1 200 OK"', "k" * 32
        )
    assert "infura.io/v3" not in caplog.text
