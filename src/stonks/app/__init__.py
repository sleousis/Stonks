"""Application service layer.

Plain-Python services that every transport (CLI, REST API, MCP server) calls.
Nothing here imports a web framework: services take and return pydantic
models / primitives and raise :mod:`stonks.app.errors` exceptions that each
transport maps onto its own error shape.
"""
