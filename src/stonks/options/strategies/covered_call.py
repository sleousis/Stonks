"""Covered call overlay (roadmap 17.5; Whaley 2002 on the CBOE BXM index;
Israelov and Nielsen 2015).

Holds round lots of each underlying (``stock_allocation`` of equity split
evenly) and writes one call per 100 shares at about ``delta`` with about
``dte`` days to run. A call is bought back at ``profit_take`` of its
credit or with ``roll_dte`` days left; the next day a new one is written.
Assigned shares are bought again the next day.
"""

from __future__ import annotations

from stonks.core.params import ParameterSpec, ParamSpace
from stonks.options.strategies._common import buy_shares, exit_intent, exit_specs, option_groups
from stonks.options.strategy import Decision, OptionDecisionContext, OptionIntent, OptionStrategy


class CoveredCallOverlay(OptionStrategy):
    id = "covered_call"
    hypothesis = (
        "Implied volatility on equity options exceeds the volatility that "
        "follows, on average, because hedgers pay for protection and for "
        "lottery-like upside. Writing calls against held shares collects that "
        "premium (Whaley's BXM, Israelov and Nielsen). We are paid by call "
        "buyers. It fails in strong rallies, where the upside is capped, and "
        "gives little cushion in crashes."
    )
    structures = ("covered_call",)

    @classmethod
    def parameter_spec(cls) -> ParamSpace:
        return [
            ParameterSpec("delta", "float", 0.30, (0.05, 0.6), description="call delta to write"),
            ParameterSpec("dte", "int", 35, (7, 90), description="target days to expiry"),
            ParameterSpec("stock_allocation", "float", 0.95, (0.1, 1.0), tunable=False),
            *exit_specs(0.5, 7),
        ]

    def decide(self, ctx: OptionDecisionContext) -> list[Decision]:
        p = self.params
        out: list[Decision] = []
        underlyings = list(ctx.history)
        for underlying in underlyings:
            shares = buy_shares(ctx, underlying, p["stock_allocation"] / len(underlyings), self.id)
            if shares is not None:
                out.append(shares)
                continue
            groups = option_groups(ctx, underlying, self.structures)
            for group in groups:
                exit_ = exit_intent(ctx, group, p["profit_take"], p["roll_dte"])
                if exit_ is not None:
                    out.append(exit_)
            if not groups and ctx.shares(underlying) >= 100:
                out.append(
                    OptionIntent(underlying, "covered_call", {"delta": p["delta"], "dte": p["dte"]})
                )
        return out
