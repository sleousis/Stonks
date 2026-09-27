"""Cash-secured puts, optionally as the wheel (roadmap 17.5).

Sells puts at about ``delta`` with about ``dte`` days on each underlying,
securing the strike with cash (``allocation`` of equity split evenly).
With ``trend_days`` set, it only sells while the close is above that
moving average (P34: check the tide). A put is bought back at
``profit_take`` of its credit or with ``roll_dte`` days left. With
``wheel`` on, shares from an assignment get covered calls until they are
called away.
"""

from __future__ import annotations

from stonks.core.params import ParameterSpec, ParamSpace
from stonks.options.strategies._common import exit_intent, exit_specs, option_groups, sma
from stonks.options.strategy import Decision, OptionDecisionContext, OptionIntent, OptionStrategy


class CashSecuredPut(OptionStrategy):
    id = "cash_secured_put"
    hypothesis = (
        "Equity puts are priced above the losses they insure on average: the "
        "variance risk premium and crash aversion make hedgers overpay "
        "(Bakshi and Kapadia; the CBOE PUT index). Selling cash-secured puts "
        "collects it with no leverage. We are paid by portfolio hedgers. It "
        "fails in sharp sell-offs, where the loss is the stock's fall less "
        "the premium; the trend filter tries to stand aside in bear markets."
    )
    structures = ("cash_secured_put", "covered_call")

    @classmethod
    def parameter_spec(cls) -> ParamSpace:
        return [
            ParameterSpec("delta", "float", 0.25, (0.05, 0.5), description="put delta to sell"),
            ParameterSpec("dte", "int", 35, (7, 90), description="target days to expiry"),
            ParameterSpec("allocation", "float", 0.9, (0.1, 1.0), tunable=False),
            ParameterSpec(
                "trend_days",
                "int",
                0,
                (0, 250),
                description="sell only above this moving average (0: always)",
            ),
            ParameterSpec("wheel", "bool", True, None, tunable=False),
            *exit_specs(0.5, 7),
        ]

    def decide(self, ctx: OptionDecisionContext) -> list[Decision]:
        p = self.params
        out: list[Decision] = []
        underlyings = list(ctx.history)
        for underlying in underlyings:
            groups = option_groups(ctx, underlying, self.structures)
            for group in groups:
                exit_ = exit_intent(ctx, group, p["profit_take"], p["roll_dte"])
                if exit_ is not None:
                    out.append(exit_)
            if groups:
                continue
            if p["wheel"] and ctx.shares(underlying) >= 100:
                out.append(
                    OptionIntent(underlying, "covered_call", {"delta": 0.30, "dte": p["dte"]})
                )
                continue
            if p["trend_days"]:
                closes = ctx.closes(underlying)
                average = sma(closes, p["trend_days"])
                if average is None or closes[-1] < average:
                    continue
            out.append(
                OptionIntent(
                    underlying,
                    "cash_secured_put",
                    {
                        "delta": p["delta"],
                        "dte": p["dte"],
                        "allocation": p["allocation"] / len(underlyings),
                    },
                )
            )
        return out
