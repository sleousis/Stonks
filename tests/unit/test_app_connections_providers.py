"""Which providers the Connect a broker list shows (PB-08, PB-03)."""

from __future__ import annotations

from stonks.app.connections import ProviderView, visible_providers


def _p(name: str, *, enabled: bool, needs_gateway: bool = False) -> ProviderView:
    return ProviderView(
        name=name,
        display_name=name,
        auth_flow="api_key",
        capabilities=[],
        credential_fields=["gateway"] if needs_gateway else ["api_key"],
        can_trade=False,
        enabled=enabled,
        has_paper=False,
        needs_gateway=needs_gateway,
    )


ALL = [
    _p("alpaca", enabled=False),
    _p("etoro", enabled=True),
    _p("fake", enabled=False),
    _p("fake_portal", enabled=True),
    _p("ibkr", enabled=True, needs_gateway=True),
]


def test_fake_providers_show_only_when_enabled():
    names = [p.name for p in visible_providers(ALL, admin=True, gateways=[])]
    assert "fake" not in names and "fake_portal" in names


def test_turned_off_providers_show_to_admins_only():
    assert [p.name for p in visible_providers(ALL, admin=False, gateways=[])] == [
        "etoro",
        "fake_portal",
        "ibkr",
    ]
    assert "alpaca" in [p.name for p in visible_providers(ALL, admin=True, gateways=[])]


def test_a_gateway_provider_lists_the_configured_gateways():
    shown = {p.name: p for p in visible_providers(ALL, admin=False, gateways=["paper", "live"])}
    assert shown["ibkr"].gateways == ["live", "paper"]
    assert shown["etoro"].gateways == []
