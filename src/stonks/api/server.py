"""Uvicorn entrypoint: ``uvicorn --factory stonks.api.server:app_factory``.

An import-string factory (rather than an app object) is what lets
``stonks serve --reload`` re-import the code in uvicorn's reloader process.
"""

from __future__ import annotations

from fastapi import FastAPI

from stonks.api.app import create_app
from stonks.config import load_settings
from stonks.logging import configure_logging


def app_factory() -> FastAPI:
    from dotenv import load_dotenv

    load_dotenv(override=False)
    settings = load_settings()
    configure_logging(level=settings.logging.level)
    return create_app(settings)
