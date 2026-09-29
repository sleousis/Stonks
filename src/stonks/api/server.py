"""Uvicorn entrypoint: ``uvicorn --factory stonks.api.server:app_factory``.

An import-string factory (rather than an app object) is what lets
``stonks serve --reload`` re-import the code in uvicorn's reloader process.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI

from stonks.api.app import create_app
from stonks.config import load_settings
from stonks.logging import configure_logging


def app_factory() -> FastAPI:
    from dotenv import load_dotenv

    load_dotenv(override=False)
    # the API never serves on the looser code defaults (no shipped risk limits)
    settings = load_settings(required=True)
    configure_logging(level=settings.logging.level)
    # uvicorn's access log prints full URLs, query strings included, which
    # can carry a job stream token; the app logs every request itself
    # (path only, see stonks.api.app._RequestLogMiddleware).
    logging.getLogger("uvicorn.access").disabled = True
    return create_app(settings)
