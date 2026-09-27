"""Trend-following vertical spreads (roadmap 17.5).

Time-series momentum picks the direction: the ``lookback`` day return of
the underlying above zero buys a bull call spread, below zero a bear put
spread. One spread per underlying at a time, ``contracts`` units, closed
at ``profit_take`` of the debit paid, with ``roll_dte`` days left, or
when the signal flips.
"""

from __future__ import annotations

from stonks.core.params import ParameterSpec, ParamSpace
from stonks.options.strategies._common import exit_intent, exit_specs, option_groups
from stonks.options.strategy import Decision, OptionDecisionContext, OptionIntent, OptionStrategy

_BULL, _BEAR = "bull_call_spread", "bear_put_spread"


class TrendVerticalSpread(OptionStrategy):
    id = "vertical_spread"
    hypothesis = (
        "An underlying's 3-12 month return predicts its next month's "
        "direction (Moskowitz, Ooi and Pedersen). A debit vertical spread "
        "expresses the view with a loss capped at the debit and a lower "
        "cost than a single option, since the short leg sells back part of "
        "the premium. We are paid by the same slow-moving flows as stock "
        "momentum. It fails at trend reversals and when the move is too "
        "small to cover the debit before expiry."
    )
    structures = (_BULL, _BEAR)

    @classmethod
    def parameter_spec(cls) -> ParamSpace:
        return [
            ParameterSpec("lookback", "int", 63, (21, 252), description="momentum window"),
            ParameterSpec("dte", "int", 45, (14, 120)),
            ParameterSpec("long_delta", "float", 0.50, (0.3, 0.7)),
            ParameterSpec("short_delta", "float", 0.25, (0.1, 0.45)),
            ParameterSpec("contracts", "int", 1, (1, 100), tunable=False),
            *exit_specs(1.0, 10),
        ]

    def decide(self, ctx: OptionDecisionContext) -> list[Decision]:
        p = self.params
        out: list[Decision] = []
        for underlying in ctx.history:
            closes = ctx.closes(underlying)
            if len(closes) <= p["lookback"]:
                continue
            momentum = closes[-1] / closes[-1 - p["lookback"]] - 1.0
            want = _BULL if momentum > 0 else _BEAR
            groups = option_groups(ctx, underlying, self.structures)
            for group in groups:
                if group.structure != want:
                    out.append(
                        OptionIntent(
                            underlying,
                            "close_group",
                            group_id=group.group_id,
                            reason="signal flipped",
                        )
                    )
                    continue
                exit_ = exit_intent(ctx, group, p["profit_take"], p["roll_dte"])
                if exit_ is not None:
                    out.append(exit_)
            if not groups:
                out.append(
                    OptionIntent(
                        underlying,
                        want,
                        {
                            "dte": p["dte"],
                            "long_delta": p["long_delta"],
                            "short_delta": p["short_delta"],
                        },
                        quantity=p["contracts"],
                    )
                )
        return out
