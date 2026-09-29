"""Structured JSON logging via structlog."""

from __future__ import annotations

import logging
import sys

import structlog

_CONFIGURED = False

#: HTTP client libraries log each request's full URL at INFO (and headers at
#: DEBUG). A URL can carry a vendor key in its query string, so these log at
#: WARNING and above whatever the app's level.
_QUIET_HTTP_LOGGERS = ("httpx", "httpx2", "httpcore", "urllib3")


def configure_logging(level: str = "INFO") -> None:
    global _CONFIGURED
    numeric = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(level=numeric, stream=sys.stderr, format="%(message)s")
    for name in _QUIET_HTTP_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(numeric),
        cache_logger_on_first_use=True,
    )
    _CONFIGURED = True


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    if not _CONFIGURED:
        configure_logging()
    return structlog.get_logger(name)
