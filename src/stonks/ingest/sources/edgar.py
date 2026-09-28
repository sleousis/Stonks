"""SEC EDGAR as a ``DataSource`` (roadmap 23.13): primary-source filings,
point in time.

Three kinds of data, all free and keyless:

- **Filings** (:meth:`EdgarDataSource.fetch_filings`): every filing in a
  company's submission history, with the SEC acceptance time as
  ``known_at`` and, for current reports (8-K), the item codes (``2.02``
  earnings, ``5.02`` officer changes, ...). The event study and the
  calendar read these.
- **Insider trades** (:meth:`EdgarDataSource.fetch_insider_filings`): the
  non-derivative lines of each Form 4, with the acceptance time of the
  Form 4 as ``known_at``: the moment the market could first see the trade.
- **Institutional holdings** (:meth:`EdgarDataSource.fetch_institutional_holdings`):
  every line of a manager's 13F holdings report, by CUSIP.

Endpoints (plain HTTP, ``requests``): ``/files/company_tickers.json``
(ticker to CIK), ``data.sec.gov/submissions/CIK##########.json`` (the
filing list), and the filing documents under ``/Archives/edgar/data``.

**Fair access** (https://www.sec.gov/os/accessing-edgar-data): every
request names the client in its User-Agent (a name and a contact address
from ``[sources.edgar] user_agent``; the source refuses to start without
one), requests are spaced by ``min_request_interval_seconds`` (the SEC
allows 10 a second), and a 429 or 5xx backs off and retries.

**Times.** ``acceptanceDateTime`` in the submissions JSON is UTC: an Apple
earnings 8-K accepted at 20:30 is the 16:30 New York release after the
close. Rows keep it as naive UTC.

The XML is parsed with the standard library, whose expat refuses entity
expansion attacks and never fetches external entities.
"""

from __future__ import annotations

import time
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, date, datetime
from typing import Any, Literal

import requests

from stonks.ingest.filing_schemas import CorporateFilingRow, InstitutionalHoldingRow
from stonks.ingest.schemas import FinancialStatementsBundle, InsiderTransactionRow, RawPriceBar
from stonks.ingest.sources.base import (
    DataSource,
    DataSourceError,
    UnsupportedCapabilityError,
)
from stonks.logging import get_logger

__all__ = [
    "EdgarDataSource",
    "EdgarError",
    "acceptance_time",
    "parse_form4",
    "parse_information_table",
    "parse_submissions",
    "symbol_of",
]

_log = get_logger("stonks.ingest.sources.edgar")

SOURCE_ID = "edgar"
INSIDER_FORMS = ("4", "4/A")
HOLDINGS_FORMS = ("13F-HR", "13F-HR/A")
#: 13F values were reported in thousands of dollars before this filing date.
_DOLLAR_VALUES_FROM = date(2023, 1, 3)
_RETRY_STATUS = frozenset({429, 500, 502, 503, 504})

Relation = Literal["officer", "director", "officer_and_director", "ten_percent_owner", "other"]


class EdgarError(DataSourceError):
    """EDGAR refused a request or sent something we cannot read."""


# ---- parsing ------------------------------------------------------------------------


def symbol_of(ticker: str) -> str:
    """The exchange symbol of a Stonks ticker (``AAPL.US`` -> ``AAPL``,
    ``BRK-B.US`` -> ``BRK-B``). EDGAR lists US issuers only."""
    base, _, suffix = ticker.strip().upper().rpartition(".")
    if not base:
        return suffix
    if suffix != "US":
        raise EdgarError(f"EDGAR covers US listings only, not {ticker!r}")
    return base


def acceptance_time(text: str) -> datetime:
    """``2026-07-30T20:30:28.000Z`` as a naive UTC datetime."""
    stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if stamp.tzinfo is not None:
        stamp = stamp.astimezone(UTC).replace(tzinfo=None)
    return stamp


def _day(text: Any) -> date | None:
    if not text:
        return None
    try:
        return date.fromisoformat(str(text)[:10])
    except ValueError:
        return None


def _archive(base_url: str, cik: str | int, accession: str, document: str = "") -> str:
    folder = f"{base_url}/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}"
    return f"{folder}/{document}" if document else folder


def parse_submissions(
    payload: dict[str, Any],
    ticker: str,
    *,
    since: date | None = None,
    until: date | None = None,
    forms: Sequence[str] | None = None,
    base_url: str = "https://www.sec.gov",
) -> list[CorporateFilingRow]:
    """The filings of a submissions payload (or one of its older pages,
    which hold the same columns at the top level), oldest first."""
    table = payload.get("filings", {}).get("recent", payload)
    cik = str(payload.get("cik") or "").lstrip("0") or None
    columns = ("accessionNumber", "form", "filingDate", "acceptanceDateTime")
    if not all(isinstance(table.get(c), list) for c in columns):
        raise EdgarError("the submissions payload has no filing table")
    n = len(table["accessionNumber"])
    rows: list[CorporateFilingRow] = []
    for i in range(n):
        form = str(table["form"][i])
        if forms is not None and form not in forms:
            continue
        accepted = acceptance_time(str(table["acceptanceDateTime"][i]))
        day = accepted.date()
        if (since is not None and day < since) or (until is not None and day > until):
            continue
        accession = str(table["accessionNumber"][i])
        filer = cik or str(int(accession.split("-")[0]))
        items_raw = str((table.get("items") or [""] * n)[i] or "")
        document = str((table.get("primaryDocument") or [""] * n)[i] or "")
        report = (table.get("reportDate") or [None] * n)[i]
        rows.append(
            CorporateFilingRow(
                accession_number=accession,
                ticker=ticker,
                issuer_cik=filer.zfill(10),
                form=form,
                filing_date=_day(table["filingDate"][i]) or day,
                known_at=accepted,
                period_of_report=_day(report),
                items=tuple(c.strip() for c in items_raw.split(",") if c.strip()),
                url=_archive(base_url, filer, accession, document) if document else None,
            )
        )
    return sorted(rows, key=lambda r: (r.known_at, r.accession_number))


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _find(el: ET.Element | None, *path: str) -> ET.Element | None:
    """The first descendant along ``path`` of local tag names (namespaces
    ignored)."""
    node = el
    for name in path:
        if node is None:
            return None
        node = next((c for c in node if _local(c.tag) == name), None)
    return node


def _text(el: ET.Element | None, *path: str) -> str | None:
    node = _find(el, *path)
    if node is None:
        return None
    value = _find(node, "value")  # Form 4 wraps most values in <value>
    text = (value if value is not None else node).text
    return text.strip() if text and text.strip() else None


def _number(el: ET.Element | None, *path: str) -> float | None:
    text = _text(el, *path)
    if text is None:
        return None
    try:
        return float(text.replace(",", ""))
    except ValueError:
        return None


def _flag(el: ET.Element | None, *path: str) -> bool:
    return (_text(el, *path) or "").lower() in ("1", "true")


def _xml(text: str | bytes) -> ET.Element:
    try:
        return ET.fromstring(text.strip() if isinstance(text, str) else text)
    except ET.ParseError as exc:
        raise EdgarError(f"unreadable XML: {exc}") from None


def _relation(owner: ET.Element | None) -> tuple[Relation, str | None]:
    rel = _find(owner, "reportingOwnerRelationship")
    officer, director = _flag(rel, "isOfficer"), _flag(rel, "isDirector")
    title = _text(rel, "officerTitle")
    if officer and director:
        return "officer_and_director", title
    if officer:
        return "officer", title
    if director:
        return "director", title
    if _flag(rel, "isTenPercentOwner"):
        return "ten_percent_owner", title
    return "other", title


def parse_form4(
    xml: str | bytes, *, ticker: str, filing: CorporateFilingRow, url: str | None = None
) -> list[InsiderTransactionRow]:
    """The non-derivative trades of one Form 4. Derivative lines (option
    grants and exercises) are left out: they are not open-market trades.
    Every row carries the Form 4's acceptance time as ``known_at``."""
    root = _xml(xml)
    owner = _find(root, "reportingOwner")
    relation, title = _relation(owner)
    name = _text(owner, "reportingOwnerId", "rptOwnerName")
    owner_cik = _text(owner, "reportingOwnerId", "rptOwnerCik")
    table = _find(root, "nonDerivativeTable")
    rows: list[InsiderTransactionRow] = []
    for line in [] if table is None else list(table):
        if _local(line.tag) != "nonDerivativeTransaction":
            continue
        traded = _day(_text(line, "transactionDate"))
        if traded is None:
            continue
        shares = _number(line, "transactionAmounts", "transactionShares")
        price = _number(line, "transactionAmounts", "transactionPricePerShare")
        side = _text(line, "transactionAmounts", "transactionAcquiredDisposedCode")
        rows.append(
            InsiderTransactionRow(
                ticker=ticker,
                transaction_date=traded,
                filing_date=filing.filing_date,
                owner_name=name,
                owner_cik=owner_cik,
                owner_relation=relation,
                owner_title=title,
                transaction_code=_text(line, "transactionCoding", "transactionCode"),
                acquired_disposed=side if side in ("A", "D") else None,
                shares=shares,
                price=price,
                value=shares * price if shares is not None and price is not None else None,
                post_transaction_amount=_number(
                    line, "postTransactionAmounts", "sharesOwnedFollowingTransaction"
                ),
                sec_link=url or filing.url,
                known_at=filing.known_at,
            )
        )
    return rows


_DISCRETION = {"SOLE": "sole", "DFND": "defined", "OTR": "other"}
_AMOUNT = {"SH": "shares", "PRN": "principal"}


def parse_information_table(
    xml: str | bytes,
    *,
    filing: CorporateFilingRow,
    filer_name: str | None = None,
    tickers: dict[str, str] | None = None,
) -> list[InstitutionalHoldingRow]:
    """Every line of a 13F information table. Values are converted to
    dollars (reports filed before 2023-01-03 gave thousands)."""
    root = _xml(xml)
    scale = 1.0 if filing.filing_date >= _DOLLAR_VALUES_FROM else 1000.0
    period = filing.period_of_report or filing.filing_date
    known = tickers or {}
    rows: list[InstitutionalHoldingRow] = []
    for line, entry in enumerate(e for e in root if _local(e.tag) == "infoTable"):
        cusip = (_text(entry, "cusip") or "").upper()
        if not cusip:
            continue
        value = _number(entry, "value")
        put_call = (_text(entry, "putCall") or "").lower() or None
        amount_type = _AMOUNT.get((_text(entry, "shrsOrPrnAmt", "sshPrnamtType") or "").upper())
        rows.append(
            InstitutionalHoldingRow(
                accession_number=filing.accession_number,
                line=line,
                filer_cik=filing.issuer_cik,
                filer_name=filer_name,
                report_period=period,
                filing_date=filing.filing_date,
                known_at=filing.known_at,
                cusip=cusip,
                issuer_name=_text(entry, "nameOfIssuer"),
                security_class=_text(entry, "titleOfClass"),
                ticker=known.get(cusip),
                amount=_number(entry, "shrsOrPrnAmt", "sshPrnamt"),
                amount_type=amount_type,  # type: ignore[arg-type]
                value_usd=value * scale if value is not None else None,
                put_call=put_call if put_call in ("put", "call") else None,  # type: ignore[arg-type]
                investment_discretion=_DISCRETION.get(  # type: ignore[arg-type]
                    (_text(entry, "investmentDiscretion") or "").upper()
                ),
            )
        )
    return rows


# ---- the source ---------------------------------------------------------------------


class EdgarDataSource(DataSource):
    source_id = SOURCE_ID

    def __init__(
        self,
        *,
        user_agent: str,
        base_url: str = "https://www.sec.gov",
        data_url: str = "https://data.sec.gov",
        timeout_seconds: float = 30.0,
        max_retries: int = 3,
        retry_backoff_seconds: float = 1.0,
        min_request_interval_seconds: float = 0.125,
        session: Any = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        agent = user_agent.strip()
        if "@" not in agent or " " not in agent:
            raise EdgarError(
                "EDGAR needs a User-Agent with a name and a contact address, e.g. "
                "'Jane Trader jane@example.com': set [sources.edgar] user_agent or "
                "STONKS_EDGAR_USER_AGENT"
            )
        self._agent = agent
        self._base = base_url.rstrip("/")
        self._data = data_url.rstrip("/")
        self._timeout = timeout_seconds
        self._retries = max(0, max_retries)
        self._backoff = retry_backoff_seconds
        self._interval = max(0.1, min_request_interval_seconds)
        self._session = session if session is not None else requests.Session()
        self._sleep = sleep
        self._clock = clock
        self._last: float | None = None
        self._ciks: dict[str, str] | None = None

    @classmethod
    def from_config(cls, cfg: Any, **kwargs: Any) -> EdgarDataSource:
        return cls(
            user_agent=cfg.user_agent,
            base_url=cfg.base_url,
            data_url=cfg.data_url,
            timeout_seconds=cfg.timeout_seconds,
            max_retries=cfg.max_retries,
            retry_backoff_seconds=cfg.retry_backoff_seconds,
            min_request_interval_seconds=cfg.min_request_interval_seconds,
            **kwargs,
        )

    # ---- HTTP -----------------------------------------------------------------------

    def _wait(self) -> None:
        now = self._clock()
        if self._last is not None:
            gap = self._interval - (now - self._last)
            if gap > 0:
                self._sleep(gap)
        self._last = self._clock()

    def _get(self, url: str) -> Any:
        headers = {"User-Agent": self._agent, "Accept-Encoding": "gzip, deflate"}
        for attempt in range(self._retries + 1):
            self._wait()
            try:
                response = self._session.get(url, headers=headers, timeout=self._timeout)
            except requests.RequestException:
                if attempt == self._retries:
                    raise
                self._sleep(self._backoff * 2**attempt)
                continue
            status = int(response.status_code)
            if status in _RETRY_STATUS and attempt < self._retries:
                _log.warning("edgar.retry", url=url, status=status, attempt=attempt)
                self._sleep(self._backoff * 2**attempt)
                continue
            if status == 404:
                raise EdgarError(f"EDGAR has no {url}")
            if status >= 400:
                raise EdgarError(f"EDGAR answered {status} for {url}")
            return response
        raise EdgarError(f"EDGAR kept failing for {url}")  # pragma: no cover

    def _json(self, url: str) -> Any:
        try:
            return self._get(url).json()
        except ValueError:
            raise EdgarError(f"EDGAR sent no JSON for {url}") from None

    def _text(self, url: str) -> bytes:
        return self._get(url).content

    # ---- lookups --------------------------------------------------------------------

    def cik_for(self, ticker: str) -> str:
        """The 10-digit CIK of a US ticker."""
        if self._ciks is None:
            payload = self._json(f"{self._base}/files/company_tickers.json")
            entries = payload.values() if isinstance(payload, dict) else payload
            self._ciks = {
                str(e["ticker"]).upper(): str(e["cik_str"]).zfill(10)
                for e in entries
                if isinstance(e, dict) and "ticker" in e and "cik_str" in e
            }
        symbol = symbol_of(ticker)
        cik = self._ciks.get(symbol) or self._ciks.get(symbol.replace("-", "."))
        if cik is None:
            raise EdgarError(f"EDGAR lists no company with ticker {symbol!r}")
        return cik

    def _submissions(
        self,
        cik: str,
        ticker: str,
        since: date | None,
        until: date | None,
        forms: Sequence[str] | None,
    ) -> tuple[dict[str, Any], list[CorporateFilingRow]]:
        """The submissions payload and its filings, reading the older pages
        EDGAR splits off when ``since`` reaches back to them."""
        payload = self._json(f"{self._data}/submissions/CIK{cik}.json")
        kw: dict[str, Any] = {"since": since, "until": until, "forms": forms}
        rows = parse_submissions(payload, ticker, base_url=self._base, **kw)
        for page in payload.get("filings", {}).get("files", []) or []:
            last = _day(page.get("filingTo"))
            first = _day(page.get("filingFrom"))
            if since is not None and last is not None and last < since:
                continue
            if until is not None and first is not None and first > until:
                continue
            older = self._json(f"{self._data}/submissions/{page['name']}")
            older.setdefault("cik", payload.get("cik"))
            rows.extend(parse_submissions(older, ticker, base_url=self._base, **kw))
        unique = {r.accession_number: r for r in rows}
        return payload, sorted(unique.values(), key=lambda r: (r.known_at, r.accession_number))

    # ---- capabilities ---------------------------------------------------------------

    def fetch_filings(
        self,
        ticker: str,
        since: date | None = None,
        until: date | None = None,
        forms: Sequence[str] | None = None,
    ) -> list[CorporateFilingRow]:
        cik = self.cik_for(ticker)
        return self._submissions(cik, ticker, since, until, forms)[1]

    def fetch_insider_filings(
        self, ticker: str, since: date | None = None, until: date | None = None
    ) -> list[InsiderTransactionRow]:
        cik = self.cik_for(ticker)
        _, filings = self._submissions(cik, ticker, since, until, INSIDER_FORMS)
        rows: list[InsiderTransactionRow] = []
        for filing in filings:
            if not filing.url:
                continue
            # the primary document is the XSL-rendered view; the raw XML
            # sits beside it without the stylesheet folder
            folder, _, document = filing.url.rpartition("/")
            if folder.rsplit("/", 1)[-1].startswith("xsl"):
                folder = folder.rsplit("/", 1)[0]
            raw = f"{folder}/{document}"
            try:
                rows.extend(parse_form4(self._text(raw), ticker=ticker, filing=filing, url=raw))
            except EdgarError as exc:
                # one unreadable report never costs the ticker's other trades
                _log.warning(
                    "edgar.form4_skipped", accession=filing.accession_number, error=str(exc)
                )
        return rows

    def fetch_institutional_holdings(
        self,
        filer_cik: str,
        since: date | None = None,
        until: date | None = None,
        tickers: dict[str, str] | None = None,
    ) -> list[InstitutionalHoldingRow]:
        cik = str(int(filer_cik)).zfill(10)
        payload, filings = self._submissions(cik, cik, since, until, HOLDINGS_FORMS)
        name = payload.get("name")
        rows: list[InstitutionalHoldingRow] = []
        for filing in filings:
            try:
                rows.extend(self._holdings_report(cik, filing, name, tickers))
            except EdgarError as exc:
                # one unreadable report never costs the filer's other quarters
                _log.warning(
                    "edgar.report_skipped", accession=filing.accession_number, error=str(exc)
                )
        return rows

    def _holdings_report(
        self,
        cik: str,
        filing: CorporateFilingRow,
        name: str | None,
        tickers: dict[str, str] | None,
    ) -> list[InstitutionalHoldingRow]:
        index = self._json(_archive(self._base, cik, filing.accession_number, "index.json"))
        document = _information_table(index)
        if document is None:
            raise EdgarError(f"no information table in {filing.accession_number}")
        xml = self._text(_archive(self._base, cik, filing.accession_number, document))
        return parse_information_table(xml, filing=filing, filer_name=name, tickers=tickers)

    # ---- what EDGAR does not serve --------------------------------------------------

    def list_tickers(self, exchange: str) -> list[str]:
        raise UnsupportedCapabilityError(f"edgar does not list exchanges ({exchange!r})")

    def fetch_prices(
        self, ticker: str, since: date | None = None, until: date | None = None
    ) -> Iterable[RawPriceBar]:
        raise UnsupportedCapabilityError(f"edgar does not serve prices ({ticker!r})")

    def fetch_fundamentals(self, ticker: str) -> FinancialStatementsBundle:
        raise UnsupportedCapabilityError(f"edgar does not serve statements here ({ticker!r})")


def _information_table(index: dict[str, Any]) -> str | None:
    """The information table's file name in a filing index: the XML that is
    not the cover page."""
    items = index.get("directory", {}).get("item", [])
    names = [str(i.get("name", "")) for i in items if isinstance(i, dict)]
    xml = [n for n in names if n.lower().endswith(".xml") and n.lower() != "primary_doc.xml"]
    return xml[0] if xml else None
