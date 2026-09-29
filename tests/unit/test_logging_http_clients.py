"""HTTP client libraries log full request URLs at INFO, and a URL can carry
a vendor key in its query string (EODHD's api_token, for one). They log at
WARNING and above only (review wave 2, item 3)."""

from __future__ import annotations

import logging

import pytest

from stonks.logging import configure_logging


@pytest.mark.parametrize("name", ["httpx", "httpx2", "httpcore", "urllib3"])
@pytest.mark.parametrize("level", ["INFO", "DEBUG"])
def test_http_client_loggers_stay_at_warning(name, level, monkeypatch):
    monkeypatch.setattr(logging.getLogger(name), "level", logging.NOTSET)
    root = logging.getLogger()
    monkeypatch.setattr(root, "level", root.level)
    configure_logging(level=level)
    root.setLevel(getattr(logging, level))  # as basicConfig does on a fresh process
    logger = logging.getLogger(name)
    assert not logger.isEnabledFor(logging.INFO)
    assert logger.isEnabledFor(logging.WARNING)
