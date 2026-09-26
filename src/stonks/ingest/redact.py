"""Scrub credentials out of error text before it is logged or persisted.

Vendors that authenticate via a query-string token (EODHD's ``api_token``)
leak that token into ``requests`` exception messages, which embed the full
request URL. Anything that formats an exception for a log line or the
``ingest_runs.error`` column must go through :func:`redact_secrets`, and
adapters re-raise via :func:`redact_exception` so the exception itself is
clean for whoever catches it next.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from urllib.parse import quote, quote_plus

REDACTED = "***"

# Common credential query-parameter names. A generic safety net for sources
# that never registered their secret explicitly.
_TOKEN_PARAM_RE = re.compile(
    r"(?i)\b(api_token|api_key|apikey|access_token|token|key|secret)=([^&\s'\"<>]+)"
)


def redact_secrets(text: str, secrets: Iterable[str] = ()) -> str:
    """Replace every occurrence of ``secrets`` (raw and URL-encoded) and
    every ``<credential-param>=<value>`` pair in ``text`` with ``***``."""
    for secret in secrets:
        if not secret:
            continue
        for variant in {secret, quote(secret, safe=""), quote_plus(secret)}:
            text = text.replace(variant, REDACTED)
    return _TOKEN_PARAM_RE.sub(lambda m: f"{m.group(1)}={REDACTED}", text)


def redact_exception(exc: BaseException, secrets: Iterable[str] = ()) -> BaseException:
    """Scrub ``exc`` and its ``__cause__`` / ``__context__`` chain in place.

    Only string-rendered ``args`` are touched, and only when they contain a
    secret, so the exception type (and therefore every ``except`` clause
    downstream) is preserved. Returns ``exc`` for ``raise redact_exception(e)``.
    """
    secrets = tuple(secrets)
    seen: set[int] = set()
    node: BaseException | None = exc
    while node is not None and id(node) not in seen:
        seen.add(id(node))
        new_args = []
        changed = False
        for arg in node.args:
            rendered = arg if isinstance(arg, str) else str(arg)
            clean = redact_secrets(rendered, secrets)
            if clean != rendered:
                new_args.append(clean)
                changed = True
            else:
                new_args.append(arg)
        if changed:
            node.args = tuple(new_args)
        node = node.__cause__ or node.__context__
    return exc


def format_exception(exc: BaseException, secrets: Iterable[str] = ()) -> str:
    """``"<Type>: <message>"`` with credentials scrubbed."""
    return redact_secrets(f"{type(exc).__name__}: {exc}", secrets)
