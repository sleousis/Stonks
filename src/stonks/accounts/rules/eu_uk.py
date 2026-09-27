"""EU and UK account rules (roadmap 19.7).

- ``priips_kid``: retail clients in the EU (PRIIPs) and the UK (its
  successor consumer disclosure rules) cannot buy a fund without a local
  key information document. Most US-domiciled ETFs have none. A buy of a
  fund (``security_type`` ``etf`` or ``fund``) domiciled outside the
  accepted area (EEA for the EU, the UK or EEA for the UK) is dropped
  unless the fund is marked ``kid_available`` (from broker rejections and
  the owner's list, ``product_documents``). Sells always pass.
  Professional clients skip it;
- ``short_disclosure``: a net short of 0.1% of issued shares must be
  reported to the regulator (0.5% is published). Each short sale is capped
  so the position stays below ``short_disclosure_threshold``. With no
  shares outstanding in the lake, the short is dropped.
"""

from __future__ import annotations

from stonks.accounts.rules import (
    AccountBook,
    AccountRule,
    AccountRuleInputs,
    AccountRulesSettings,
    OrderView,
    Verdict,
    register_account_rule,
)

#: EEA countries by ISIN prefix: funds domiciled here are UCITS with an EU
#: key information document.
EEA = frozenset(
    {
        "AT", "BE", "BG", "CY", "CZ", "DE", "DK", "EE", "ES", "FI", "FR", "GR", "HR", "HU",
        "IE", "IS", "IT", "LI", "LT", "LU", "LV", "MT", "NL", "NO", "PL", "PT", "RO", "SE",
        "SI", "SK",
    }
)  # fmt: skip
#: Domiciles each jurisdiction accepts without an explicit document flag.
ACCEPTED: dict[str, frozenset[str]] = {"eu": EEA, "uk": EEA | {"GB", "JE", "GG", "IM"}}


@register_account_rule
class PriipsKid(AccountRule):
    name = "priips_kid"
    order = 80
    jurisdictions = frozenset({"eu", "uk"})

    def check(
        self,
        view: OrderView,
        qty: float,
        book: AccountBook,
        inputs: AccountRuleInputs,
        settings: AccountRulesSettings,
    ) -> Verdict | None:
        profile = inputs.profile
        if view.side != "buy" or not view.opening or profile.client_class == "professional":
            return None
        facts = inputs.instruments.get(view.ticker)
        if facts is None or not facts.is_fund:
            return None
        flagged = inputs.kid_available.get(view.ticker)
        if flagged is True:
            return None
        domicile = facts.domicile
        if flagged is None and domicile in ACCEPTED[profile.jurisdiction]:
            return None
        where = domicile or "unknown"
        return self.verdict(
            view,
            "drop",
            0.0,
            f"{view.ticker} is a fund domiciled in {where} with no key information document "
            f"for {profile.jurisdiction.upper()} retail clients",
        )


@register_account_rule
class ShortDisclosure(AccountRule):
    name = "short_disclosure"
    order = 90
    jurisdictions = frozenset({"eu", "uk"})

    def check(
        self,
        view: OrderView,
        qty: float,
        book: AccountBook,
        inputs: AccountRuleInputs,
        settings: AccountRulesSettings,
    ) -> Verdict | None:
        if view.side != "sell" or not view.opening:
            return None
        facts = inputs.instruments.get(view.ticker)
        shares = facts.shares_outstanding if facts is not None else None
        if not shares or shares <= 0:
            return self.verdict(
                view, "drop", 0.0, "shares outstanding unknown; cannot keep the short undisclosed"
            )
        limit = settings.short_disclosure_threshold * shares
        short_now = max(-book.positions.get(view.ticker, 0.0), 0.0)
        # stay strictly below the reporting line
        room = max(limit - short_now, 0.0) * (1 - 1e-9)
        return self.clip_to(
            view,
            qty,
            room,
            f"a net short must stay below {settings.short_disclosure_threshold:.2%} of "
            f"{shares:,.0f} issued shares",
        )
