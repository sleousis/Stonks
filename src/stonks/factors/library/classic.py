"""A few classic price factors with a stated hypothesis (roadmap 22.2):
the starting points for :class:`FactorStrategy` and tear sheets."""

from __future__ import annotations

from stonks.factors.base import ExpressionFactor, Factor, Provenance

_JEGADEESH_TITMAN = Provenance(
    "Jegadeesh and Titman (1993), Returns to buying winners and selling losers: "
    "implications for stock market efficiency, Journal of Finance 48(1)",
    published=1993,
    sample_start=1965,
    sample_end=1989,
    reported="6-month formation and holding: 12.01% a year compounded",
)


def factors() -> list[Factor]:
    return [
        ExpressionFactor(
            "mom_12_1",
            "Ref($close, 21)/Ref($close, 252)-1",
            description="12-month return skipping the last month",
            family="momentum",
            hypothesis=(
                "Investors underreact to news that arrives slowly, so the past year's "
                "winners keep winning for some months (Jegadeesh and Titman 1993). The "
                "last month is skipped for its short-term reversal. Fails in momentum "
                "crashes, when last year's losers rebound hardest."
            ),
            provenance=_JEGADEESH_TITMAN,
        ),
        ExpressionFactor(
            "mom_6_1",
            "Ref($close, 21)/Ref($close, 126)-1",
            description="6-month return skipping the last month",
            family="momentum",
            hypothesis=(
                "The medium-term version of 12-1 momentum: underreaction to slow news. "
                "Fails in sharp market reversals."
            ),
            provenance=_JEGADEESH_TITMAN,
        ),
        ExpressionFactor(
            "reversal_1m",
            "Ref($close, 21)/$close-1",
            description="minus the last month's return",
            family="reversal",
            hypothesis=(
                "Liquidity providers are paid to absorb one-month overreaction, so last "
                "month's losers bounce (Jegadeesh 1990). Fails when the moves carry real "
                "news, and after trading costs in illiquid names."
            ),
            provenance=Provenance(
                "Jegadeesh (1990), Evidence of predictable behavior of security returns, "
                "Journal of Finance 45(3)",
                published=1990,
                sample_start=1934,
                sample_end=1987,
                reported="extreme decile portfolios on predicted monthly returns differ "
                "by about 2.5% a month",
            ),
        ),
        ExpressionFactor(
            "low_vol_60",
            "Std($close/Ref($close, 1)-1, 60)",
            description="60-bar volatility of daily returns",
            family="volatility",
            direction=-1,
            hypothesis=(
                "Leverage-constrained investors bid up risky stocks, so calmer stocks earn "
                "more per unit of risk (the low-volatility anomaly). Fails in speculative "
                "rallies led by high-beta names."
            ),
            provenance=Provenance(
                "Ang, Hodrick, Xing and Zhang (2006), The cross-section of volatility and "
                "expected returns, Journal of Finance 61(1)",
                published=2006,
                sample_start=1963,
                sample_end=2000,
                reported="highest minus lowest idiosyncratic volatility quintile about "
                "-1% a month (the paper measures volatility net of the Fama-French factors)",
            ),
        ),
        ExpressionFactor(
            "dist_52w_high",
            "$close/Max($high, 252)",
            description="close over the 52-week high",
            family="momentum",
            hypothesis=(
                "Investors anchor on the 52-week high and underreact near it, so stocks "
                "close to their high keep rising (George and Hwang 2004). Fails at "
                "market tops."
            ),
            provenance=Provenance(
                "George and Hwang (2004), The 52-week high and momentum investing, "
                "Journal of Finance 59(5)",
                published=2004,
                sample_start=1963,
                sample_end=2001,
                reported="nearness to the 52-week high explains a large part of momentum "
                "profits and does not reverse in the long run",
            ),
        ),
        ExpressionFactor(
            "size_dv_60",
            "Log(Mean($close*$volume, 60))",
            description="log of the 60-bar mean dollar volume, a size proxy",
            family="size",
            direction=-1,
            hypothesis=(
                "Small, thinly traded names carry more liquidity and information risk, "
                "so they should earn a premium (Banz 1981, Amihud 2002). Dollar volume "
                "stands in for market cap, which the lake may not hold point in time. "
                "Fails in flights to quality, when large liquid names lead. The style "
                "risk model uses it as its size exposure."
            ),
        ),
    ]
