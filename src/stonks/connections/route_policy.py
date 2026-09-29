"""Routes Stonks refuses: a broker that an aggregator reaches in a way the
broker's own terms forbid.

An aggregator such as SnapTrade lists many brokers in its portal, and some
of them it reaches with the person's login over the broker's private web
API. When the broker's terms forbid that, Stonks never syncs the account,
even though the aggregator offers it: the person's account would be at
risk. Each entry says why, and what to do instead.

DEGIRO: SnapTrade marks its DEGIRO link ``UNOFFICIAL_API``. DEGIRO's
client agreement forbids automated tools (art. 5.6) and any use of the
login by a third party (art. 3.2 and 7.2.1). See ``docs/design/degiro.md``.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RefusedRoute:
    #: The connection provider (``snaptrade``).
    provider: str
    #: The broker as people know it.
    broker: str
    #: Folded names the provider reports for that broker's accounts.
    institutions: tuple[str, ...]
    reason: str
    instead: str


REFUSED: tuple[RefusedRoute, ...] = (
    RefusedRoute(
        provider="snaptrade",
        broker="DEGIRO",
        institutions=("degiro", "flatexdegiro", "flatex degiro"),
        reason=(
            "SnapTrade reaches DEGIRO with your login over an unofficial route, and DEGIRO's "
            "client agreement forbids automated tools and sharing your login."
        ),
        instead="Import your DEGIRO CSV exports instead (Broker connections, Import a CSV).",
    ),
)


def refused_route(provider: str, institution: str | None) -> RefusedRoute | None:
    """The refusal for an account of ``institution`` at ``provider``, if any."""
    name = " ".join((institution or "").lower().split())
    if not name:
        return None
    for route in REFUSED:
        if route.provider == provider and any(i in name for i in route.institutions):
            return route
    return None
