"""``[brokers.ibkr]`` settings (roadmap 19.4).

Example::

    [brokers.ibkr]
    allow_live = false

    [brokers.ibkr.gateways.paper]
    host = "ib-gateway-paper"
    port = 4004
    mode = "paper"
    portfolios = ["pf_default"]

    [brokers.ibkr.gateways.live]
    host = "ib-gateway-live"
    port = 4003
    mode = "live"
    portfolios = ["pf_live"]
    account_id = "U1234567"   # the expected account, not a secret

Only non-secret settings live here. A username, password or token in TOML
is refused: the IBKR login lives in the gateway's Docker secret files, and
the optional Flex token comes from ``STONKS_IBKR_FLEX_TOKEN``.
"""

from __future__ import annotations

from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

GatewayMode = Literal["paper", "live"]

#: Keys that would hold a credential. Never accepted from TOML.
_SECRET_KEYS = ("username", "user_id", "password", "flex_token", "token")


def _refuse_secrets(data: Any, where: str) -> Any:
    if not isinstance(data, dict):
        return data
    table = cast("dict[str, Any]", data)
    found = [k for k in _SECRET_KEYS if k in table]
    if found:
        for key in found:
            table[key] = "**********"
        names = ", ".join(f"{where}.{k}" for k in found)
        raise ValueError(
            f"{names} must not be set in config: the IBKR login lives in the gateway's "
            "Docker secret files and the Flex token in STONKS_IBKR_FLEX_TOKEN"
        )
    return table


class IbkrGatewayConfig(BaseModel):
    """One IB Gateway container Stonks can reach."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    host: str = Field(min_length=1)
    port: int = Field(ge=1, le=65535)
    mode: GatewayMode
    #: The portfolios that trade through this gateway. A long outage pauses
    #: their auto subscriptions, and alerts go to their owners.
    portfolios: list[str] = Field(default_factory=list)
    #: The account the gateway must be logged in to (``DU...`` for paper,
    #: ``U...`` for live). Checked by the adapter on connect (19.2).
    account_id: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _no_secrets(cls, data: Any) -> Any:
        return _refuse_secrets(data, "brokers.ibkr.gateways.<name>")

    @model_validator(mode="after")
    def _account_matches_mode(self) -> IbkrGatewayConfig:
        acct = self.account_id
        if acct is None:
            return self
        paper = acct.upper().startswith("DU")
        if self.mode == "paper" and not paper:
            raise ValueError("a paper gateway's account_id starts with DU")
        if self.mode == "live" and paper:
            raise ValueError("a live gateway cannot use a paper (DU) account_id")
        return self


class IbkrClientIds(BaseModel):
    """The fixed TWS API client id of each process role."""

    model_config = ConfigDict(extra="forbid")

    tick: int = Field(default=11, ge=0)
    sync: int = Field(default=12, ge=0)
    health: int = Field(default=13, ge=0)


class IbkrHealthSettings(BaseModel):
    """How the ``broker_health`` job judges a gateway (roadmap 19.4)."""

    model_config = ConfigDict(extra="forbid")

    #: Seconds a probe may take before the gateway counts as down.
    probe_timeout_seconds: float = Field(default=2.0, gt=0)
    #: Failed checks in a row before the owners get a high-urgency push.
    alert_after_failures: int = Field(default=2, ge=1)
    #: Trading sessions the gateway may stay down before the auto
    #: subscriptions of its portfolios pause (resume needs a second factor).
    #: A real fault (login refused, wrong account) pauses at once.
    pause_after_sessions: int = Field(default=2, ge=1)
    #: The exchange calendar that counts sessions.
    calendar: str = "XNYS"


class IbkrBrokerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    #: Real-money orders need this and a live stage. Off by default.
    allow_live: bool = False
    client_ids: IbkrClientIds = Field(default_factory=IbkrClientIds)
    connect_timeout_seconds: float = Field(default=5.0, gt=0)
    gateways: dict[str, IbkrGatewayConfig] = Field(default_factory=dict[str, IbkrGatewayConfig])
    health: IbkrHealthSettings = Field(default_factory=IbkrHealthSettings)

    @model_validator(mode="before")
    @classmethod
    def _no_secrets(cls, data: Any) -> Any:
        return _refuse_secrets(data, "brokers.ibkr")

    @model_validator(mode="after")
    def _one_gateway_per_portfolio(self) -> IbkrBrokerConfig:
        seen: dict[str, str] = {}
        for name, gw in self.gateways.items():
            for pid in gw.portfolios:
                if pid in seen:
                    raise ValueError(
                        f"portfolio {pid!r} is listed on gateways {seen[pid]!r} and {name!r}"
                    )
                seen[pid] = name
        return self
