"""Countries of the economic calendar (roadmap 20.9).

Economic releases carry an ISO 3166-1 alpha-2 country, or a region such as
``EU`` for the euro area. A person's economic alerts default to the
countries of their portfolios' base currencies, through
:func:`country_for_currency`.
"""

from __future__ import annotations

import re

#: The currency's home economy. The euro maps to the euro area.
CURRENCY_COUNTRY: dict[str, str] = {
    "USD": "US",
    "EUR": "EU",
    "GBP": "GB",
    "JPY": "JP",
    "CHF": "CH",
    "CAD": "CA",
    "AUD": "AU",
    "NZD": "NZ",
    "CNY": "CN",
    "HKD": "HK",
    "SEK": "SE",
    "NOK": "NO",
    "DKK": "DK",
    "SGD": "SG",
    "INR": "IN",
    "KRW": "KR",
    "BRL": "BR",
    "MXN": "MX",
    "ZAR": "ZA",
    "PLN": "PL",
}

#: Countries the console offers, in display order. Any valid code is
#: accepted: this list only fills the picker.
COUNTRY_LABELS: dict[str, str] = {
    "US": "United States",
    "EU": "Euro area",
    "DE": "Germany",
    "FR": "France",
    "IT": "Italy",
    "ES": "Spain",
    "GB": "United Kingdom",
    "CH": "Switzerland",
    "JP": "Japan",
    "CN": "China",
    "CA": "Canada",
    "AU": "Australia",
    "NZ": "New Zealand",
    "SE": "Sweden",
    "NO": "Norway",
    "DK": "Denmark",
    "HK": "Hong Kong",
    "SG": "Singapore",
    "IN": "India",
    "KR": "South Korea",
    "BR": "Brazil",
    "MX": "Mexico",
    "ZA": "South Africa",
    "PL": "Poland",
}

#: Where economic alerts go when nothing else says.
FALLBACK_COUNTRY = "US"

_CODE = re.compile(r"^[A-Z]{2,3}$")


def is_country_code(code: str) -> bool:
    """Two or three upper-case letters, the form ``economic_events`` stores."""
    return bool(_CODE.match(code))


def country_for_currency(currency: str) -> str | None:
    return CURRENCY_COUNTRY.get(currency.strip().upper())
