"""REST API (FastAPI) over the :mod:`stonks.app` service layer."""

from stonks.api.app import create_app

__all__ = ["create_app"]
