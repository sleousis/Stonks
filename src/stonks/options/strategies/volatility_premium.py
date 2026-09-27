"""Volatility risk premium with iron condors (roadmap 17.5; Sinclair,
*Volatility Trading* and *Positional Option Trading*; Natenberg).

Sells an iron condor (short ``short_delta`` strangle, long ``wing_delta``
wings) on an underlying when its at-the-money implied vol is at least
``iv_ratio`` times its realized vol over ``rv_days``. One condor per
underlying, ``contracts`` units, closed at ``profit_take`` of the credit
or with ``roll_dte`` days left.
"""

from __future__ import annotations

from stonks.core.params import ParameterSpec, ParamSpace
from stonks.options.strategies._common import atm_iv, exit_intent, exit_specs, option_groups
from stonks.options.strategy import Decision, OptionDecisionContext, OptionIntent, OptionStrategy


class VolatilityPremiumCondor(OptionStrategy):
    id = "vol_premium_condor"
    hypothesis = (
        "Implied volatility exceeds the realized volatility that follows "
        "most of the time: option buyers pay for insurance and for convexity "
        "(Sinclair; Natenberg; Carr and Wu). Selling a defined-risk iron "
        "condor only when implied is rich against recent realized harvests "
        "that premium with the crash loss capped by the wings. We are paid "
        "by hedgers. It fails when realized volatility jumps above implied, "
        "as in a crash, and the wings then set the loss."
    )
    structures = ("iron_condor",)

    @classmethod
    def parameter_spec(cls) -> ParamSpace:
        return [
            ParameterSpec(
                "iv_ratio", "float", 1.2, (0.8, 2.0), description="implied over realized"
            ),
            ParameterSpec("rv_days", "int", 21, (10, 63)),
            ParameterSpec("dte", "int", 45, (14, 90)),
            ParameterSpec("short_delta", "float", 0.16, (0.05, 0.35)),
            ParameterSpec("wing_delta", "float", 0.05, (0.01, 0.2)),
            ParameterSpec("contracts", "int", 1, (1, 100), tunable=False),
            *exit_specs(0.5, 21),
        ]

    def decide(self, ctx: OptionDecisionContext) -> list[Decision]:
        p = self.params
        out: list[Decision] = []
        for underlying in ctx.history:
            groups = option_groups(ctx, underlying, self.structures)
            for group in groups:
                exit_ = exit_intent(ctx, group, p["profit_take"], p["roll_dte"])
                if exit_ is not None:
                    out.append(exit_)
            if groups:
                continue
            implied = atm_iv(ctx, underlying, p["dte"])
            realized = ctx.realized_vol(underlying, p["rv_days"])
            if implied is None or realized is None or implied < p["iv_ratio"] * realized:
                continue
            out.append(
                OptionIntent(
                    underlying,
                    "iron_condor",
                    {
                        "dte": p["dte"],
                        "short_delta": p["short_delta"],
                        "wing_delta": p["wing_delta"],
                    },
                    quantity=p["contracts"],
                    reason=f"iv {implied:.2f} vs rv {realized:.2f}",
                )
            )
        return out
