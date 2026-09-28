"""Published anomalies that EODHD bars and statements can build (roadmap
23.13), each with the paper it comes from.

Modelled on Chen and Zimmermann's open-source asset pricing project: every
signal names its paper, sample years and reported result, so a tear sheet
can ask whether it still works after publication (McLean and Pontiff 2016
found returns about a third lower). Some published factors live in other
sets because they came first: 12-1 momentum, short-term reversal, low
volatility and the 52-week high in ``classic``, book to market, accruals,
net operating assets and the F-score in ``fundamentals``.

Honest limits: the papers use CRSP and Compustat over long samples. EODHD
history is shorter and its universe differs, so these are ports of the
definitions, not replications. Most lake history falls after publication.
"""

from __future__ import annotations

from stonks.factors.base import ExpressionFactor, Factor, Provenance
from stonks.factors.statements import (
    StatementFactor,
    asset_growth,
    gross_profitability,
    net_share_issuance,
)


def factors() -> list[Factor]:
    return [
        ExpressionFactor(
            "max_return_21",
            "Max($close/Ref($close, 1)-1, 21)",
            description="largest daily return of the last month",
            family="lottery",
            direction=-1,
            hypothesis=(
                "Investors overpay for lottery-like stocks with extreme daily gains, so "
                "last month's biggest one-day winners earn less. Fails in speculative "
                "rallies."
            ),
            provenance=Provenance(
                "Bali, Cakici and Whitelaw (2011), Maxing out: stocks as lotteries and the "
                "cross-section of expected returns, Journal of Financial Economics 99(2)",
                published=2011,
                sample_start=1962,
                sample_end=2005,
                reported="lowest minus highest MAX decile over 1% a month",
            ),
        ),
        ExpressionFactor(
            "amihud_illiquidity",
            "Mean(Abs($close/Ref($close, 1)-1)/($close*$volume), 21)",
            description="mean absolute daily return per dollar traded, last month",
            family="liquidity",
            hypothesis=(
                "Investors want to be paid for holding stocks that move a lot per dollar "
                "traded, so illiquid names earn a premium. Fails in liquidity crises, "
                "when illiquid names fall hardest, and after trading costs."
            ),
            provenance=Provenance(
                "Amihud (2002), Illiquidity and stock returns: cross-section and "
                "time-series effects, Journal of Financial Markets 5(1)",
                published=2002,
                sample_start=1963,
                sample_end=1997,
                reported="expected illiquidity has a positive and significant effect on "
                "expected excess returns",
            ),
        ),
        ExpressionFactor(
            "seasonality_12m",
            "Ref($close, 231)/Ref($close, 252)-1",
            description="the return of the month one year before the coming month",
            family="seasonality",
            hypothesis=(
                "Some stocks do well in the same calendar month year after year, so the "
                "return of the coming month a year ago predicts it. Fails when the "
                "seasonal driver (a fiscal calendar, a flow) goes away."
            ),
            provenance=Provenance(
                "Heston and Sadka (2008), Seasonality in the cross-section of stock "
                "returns, Journal of Financial Economics 87(2)",
                published=2008,
                sample_start=1965,
                sample_end=2002,
                reported="returns at annual lags predict the same calendar month's return "
                "for up to 20 years",
            ),
        ),
        ExpressionFactor(
            "long_term_reversal",
            "Ref($close, 252)/Ref($close, 1260)-1",
            description="return from five years ago to one year ago",
            family="reversal",
            direction=-1,
            hypothesis=(
                "Investors overreact to long runs of news, so multi-year losers beat "
                "multi-year winners. Fails when the losers were cheap for a reason."
            ),
            provenance=Provenance(
                "De Bondt and Thaler (1985), Does the stock market overreact?, Journal "
                "of Finance 40(3)",
                published=1985,
                sample_start=1926,
                sample_end=1982,
                reported="36-month losers beat winners by about 25% over the next three years",
            ),
        ),
        StatementFactor(
            "asset_growth",
            asset_growth,
            description="growth of total assets over the last fiscal year",
            family="investment",
            direction=-1,
            hypothesis=(
                "Firms that grow their assets fast overinvest and investors extrapolate "
                "the growth, so high asset growth predicts lower returns. Fails when "
                "growth is funded by real, lasting demand."
            ),
            provenance=Provenance(
                "Cooper, Gulen and Schill (2008), Asset growth and the cross-section of "
                "stock returns, Journal of Finance 63(4)",
                published=2008,
                sample_start=1968,
                sample_end=2003,
                reported="low minus high asset growth decile about 20% a year, equal weighted",
            ),
        ),
        StatementFactor(
            "gross_profitability",
            gross_profitability,
            description="gross profit over total assets, last fiscal year",
            family="quality",
            hypothesis=(
                "Gross profit is the cleanest measure of economic profit, and the market "
                "underprices profitable firms. Fails in junk rallies."
            ),
            provenance=Provenance(
                "Novy-Marx (2013), The other side of value: the gross profitability "
                "premium, Journal of Financial Economics 108(1)",
                published=2013,
                sample_start=1963,
                sample_end=2010,
                reported="value-weighted high minus low gross profitability quintile 0.31% a month",
                t_stat=2.49,
            ),
        ),
        StatementFactor(
            "net_share_issuance",
            net_share_issuance,
            description="log change in shares outstanding over the last fiscal year, net of splits",
            family="investment",
            direction=-1,
            hypothesis=(
                "Managers issue shares when they are overpriced and buy back when they "
                "are cheap, so net issuers underperform. Fails when issuance funds "
                "growth the market underrates."
            ),
            provenance=Provenance(
                "Pontiff and Woodgate (2008), Share issuance and cross-sectional returns, "
                "Journal of Finance 63(2)",
                published=2008,
                sample_start=1970,
                sample_end=2003,
                reported="after 1970 share issuance predicts returns more strongly than "
                "size, book to market or momentum",
            ),
            tables=("balance_sheet", "stock_splits"),
        ),
    ]
