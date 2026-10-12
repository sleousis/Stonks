"""Reproducibility manifest (BL-06): what it takes to rerun a lab result.

:func:`build_manifest` is a pure builder the runner calls once per run. The
manifest is stored in ``lab_runs.manifest_json`` and in the artifact's
``meta.json``:

- ``git_sha`` / ``git_dirty``: the checkout's commit, else the
  ``STONKS_GIT_SHA`` the image was built with (``None`` when neither);
- ``source_hash``: sha256 over the package's ``.py`` files
  (:mod:`stonks.code_identity`), so a deploy without ``.git`` still has a
  code identity;
- ``stonks_version``, ``python_version`` and ``libraries`` (versions of the
  numeric and optimisation stack, plus ``uv.lock``, the sha256 of the lock
  file when the checkout has one);
- ``config_hash``: sha256 of the resolved settings JSON, secrets excluded;
- ``data_fingerprint``: per ticker, every bar from the first stored one to
  the window end, corporate actions and statement versions, plus every
  lake table the strategy declares in ``data_tables`` (see
  :func:`data_fingerprint`), and one sha256 over all of it;
- ``costs``: the cost-model settings in force;
- ``shorting``: the margin model and borrow fees, only for a dataset that
  may short (roadmap 16.4);
- ``lots``: the lot settings, only for a dataset that rounds orders to
  lots (roadmap 23.1);
- ``backtest``: the fill model and settlement, construction, benchmark and
  embargo of every backtest (``lab verify`` rebuilds the dataset from it);
- ``dataset``: universe, windows and interval;
- ``seeds``: the tuner's and every seeded survival test's;
- ``recipe``: what the search was (the suite with its options, the tuner
  with its seed and options, the objective, the budget, the heatmap and the
  pinned params), when the runner passes it.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import platform
import re
import subprocess
from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime, timedelta
from importlib import metadata
from pathlib import Path
from typing import Any

from stonks.code_identity import source_hash
from stonks.logging import get_logger

_log = get_logger("stonks.lab.manifest")

#: Setting names whose values are never hashed (credentials, tokens).
_SECRET_KEY = re.compile(r"(key|secret|token|password|passphrase|credential)", re.IGNORECASE)
_LIBRARIES = (
    "numpy",
    "pandas",
    "scipy",
    "duckdb",
    "scikit-learn",
    "pydantic",
    "optuna",
    "cvxpy",
    "statsmodels",
    "arch",
    "exchange-calendars",
    "QuantLib",
)
_REPO_DIR = Path(__file__).resolve().parent
#: The lock file of a checkout (absent from an installed image).
_UV_LOCK = _REPO_DIR.parents[2] / "uv.lock"
#: The commit a deployed image was built from (Dockerfile ``ARG``).
GIT_SHA_ENV = "STONKS_GIT_SHA"


def build_manifest(
    settings: Any,
    dataset: Any,
    seeds: Mapping[str, Any],
    *,
    strategy: Any = None,
    params: Mapping[str, Any] | None = None,
    recipe: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The run's manifest. Every part degrades to ``None`` instead of
    raising, so a manifest problem never blocks a lab run. ``strategy``
    (a class or an instance) and its ``params`` name the extra lake tables
    the fingerprint covers (:func:`strategy_data_tables`). ``recipe`` is
    :func:`build_recipe`'s record of the search."""
    tables = strategy_data_tables(strategy, params) if strategy is not None else ()
    manifest: dict[str, Any] = {
        **git_info(),
        "source_hash": _safe_source_hash(),
        "stonks_version": _stonks_version(),
        "python_version": platform.python_version(),
        "libraries": library_versions(),
        "config_hash": config_hash(settings),
        "data_fingerprint": _safe_fingerprint(dataset, tables),
        "costs": _costs(settings, dataset),
        "dataset": dataset_summary(dataset),
        "seeds": _jsonable(dict(seeds)),
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        **_shorting(dataset),
        **_lots(dataset),
        **_backtest(dataset),
    }
    if recipe is not None:
        manifest["recipe"] = _jsonable(dict(recipe))
    return manifest


def _backtest(dataset: Any) -> dict[str, Any]:
    """What every backtest of the run used beyond costs, lots and shorting.
    Its presence also tells ``lab verify`` that a missing ``lots`` or
    ``shorting`` means none (an older manifest falls back to settings)."""
    if dataset is None or not hasattr(dataset, "embargo_bars"):
        return {}
    return {
        "backtest": {
            "execution": _jsonable(getattr(dataset, "execution", None)),
            "construction": _jsonable(getattr(dataset, "construction", None)),
            "benchmark": getattr(dataset, "benchmark", "auto"),
            "embargo_bars": int(getattr(dataset, "embargo_bars", 0) or 0),
        }
    }


def _safe_source_hash() -> str | None:
    try:
        return source_hash()
    except OSError as exc:  # never block a lab run on the manifest
        _log.warning("lab.manifest.source_hash_failed", error=str(exc))
        return None


def _lots(dataset: Any) -> dict[str, Any]:
    lots = getattr(dataset, "lots", None)
    if lots is None or getattr(lots, "profile", "fractional") == "fractional":
        return {}
    return {"lots": _jsonable(lots)}


def _shorting(dataset: Any) -> dict[str, Any]:
    shorting = getattr(dataset, "shorting", None)
    return {} if shorting is None else {"shorting": _jsonable(shorting)}


# ---- code identity -------------------------------------------------------------


def git_info(cwd: Path | None = None) -> dict[str, Any]:
    """``{"git_sha", "git_dirty"}`` of the checkout holding this package
    (or ``cwd``). Without git or a checkout (a deployed image), the sha is
    ``STONKS_GIT_SHA`` from the environment and the dirty flag ``None``;
    both ``None`` when that is unset too."""
    where = str(cwd or _REPO_DIR)
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=where, capture_output=True, text=True, timeout=10
        )
        if sha.returncode != 0:
            return _env_git_info()
        status = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            cwd=where,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return _env_git_info()
    dirty = bool(status.stdout.strip()) if status.returncode == 0 else None
    return {"git_sha": sha.stdout.strip() or None, "git_dirty": dirty}


def _env_git_info() -> dict[str, Any]:
    sha = os.environ.get(GIT_SHA_ENV, "").strip()
    return {"git_sha": sha or None, "git_dirty": None}


def library_versions() -> dict[str, str | None]:
    """Versions of the libraries that shape results (``None`` when one is
    not installed), plus ``uv.lock``: the lock file's sha256, which pins
    every other dependency (``None`` outside a checkout)."""
    out: dict[str, str | None] = {}
    for name in _LIBRARIES:
        try:
            out[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            out[name] = None
    out["uv.lock"] = _lock_hash()
    return out


def _lock_hash() -> str | None:
    try:
        data = _UV_LOCK.read_bytes()
    except OSError:
        return None
    return hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest()


def code_identity_changed(stored: Mapping[str, Any]) -> bool | None:
    """Whether the code differs from the one a manifest recorded: by git
    sha when both sides have one, else by :func:`source_hash`; ``None``
    when neither can be compared."""
    stored_sha = stored.get("git_sha")
    now_sha = git_info().get("git_sha")
    if stored_sha is not None and now_sha is not None:
        return bool(stored_sha != now_sha)
    stored_source = stored.get("source_hash")
    if stored_source is not None:
        now_source = _safe_source_hash()
        if now_source is not None:
            return bool(stored_source != now_source)
    return None


def _stonks_version() -> str:
    from stonks import __version__

    return __version__


# ---- configuration --------------------------------------------------------------


def canonical_settings_json(settings: Any) -> str:
    """Sorted-key JSON of the resolved settings with every secret removed:
    keys that name a credential are dropped, and any other value equal to a
    configured secret (e.g. a webhook URL with an embedded token) too."""
    dump = settings.model_dump(mode="json") if hasattr(settings, "model_dump") else dict(settings)
    secrets = set(_configured_secrets(settings))
    return json.dumps(_scrub(dump, secrets), sort_keys=True, separators=(",", ":"))


def config_hash(settings: Any) -> str | None:
    if settings is None:
        return None
    return hashlib.sha256(canonical_settings_json(settings).encode()).hexdigest()


def _configured_secrets(settings: Any) -> list[str]:
    try:
        from stonks.config import configured_secrets

        return configured_secrets(settings)
    except Exception:  # not a stonks Settings object
        return []


def _scrub(obj: Any, secrets: set[str]) -> Any:
    if isinstance(obj, dict):
        return {
            k: _scrub(v, secrets)
            for k, v in obj.items()
            if not _SECRET_KEY.search(str(k)) and not (isinstance(v, str) and v in secrets)
        }
    if isinstance(obj, list):
        return [_scrub(v, secrets) for v in obj if not (isinstance(v, str) and v in secrets)]
    return obj


def _costs(settings: Any, dataset: Any) -> Any:
    costs = getattr(dataset, "costs", None)
    if costs is None:
        costs = getattr(getattr(settings, "backtest", None), "costs", None)
    return _jsonable(costs)


# ---- data identity ----------------------------------------------------------------


def dataset_summary(dataset: Any) -> dict[str, Any] | None:
    """Universe, windows and interval of a ``LabDataset`` (``None`` otherwise)."""
    try:
        summary = {
            "universe": list(dataset.universe),
            "full_window": [str(d) for d in dataset.full_window],
            "train_window": [str(d) for d in dataset.train_window],
            "val_window": [str(d) for d in dataset.val_window],
            "interval": _interval_code(dataset),
        }
        universe_id = getattr(dataset, "universe_id", None)
        if isinstance(universe_id, str):
            summary["universe_id"] = universe_id
        sessions = tuple(getattr(dataset, "window_sessions", ()) or ())
        if sessions:  # windows split by session (roadmap 21.3.1)
            summary["sessions"] = len(sessions)
        return summary
    except Exception:
        return None


#: Calendar days of warm-up history before the window start that older
#: fingerprints covered (RS-18). A fingerprint now covers every bar from
#: the first stored one (``warmup_days`` is ``None``), since a strategy
#: with a long look-back or a full-history feature reads them all. A
#: stored fingerprint is recomputed with the warm-up it was made with.
FINGERPRINT_WARMUP_DAYS = 550

_BAR_COLUMNS = ("open", "high", "low", "close", "adj_close", "volume")


@dataclasses.dataclass(frozen=True)
class _TableSpec:
    """How the fingerprint reads one declared lake table: the date column
    that bounds it by the window end, its ticker column (``None`` for a
    table that is not per ticker, which is hashed whole), and its
    ``*_versions`` table with ``known_at`` (migrations 018 and 025)."""

    date_column: str | None
    ticker_column: str | None = None
    versions: str | None = None


#: The lake tables a strategy may declare in ``data_tables``. A table not
#: listed here is hashed whole, unbounded.
DATA_TABLES: dict[str, _TableSpec] = {
    "macro_indicators": _TableSpec("observation_date", None, "macro_indicators_versions"),
    "defi_tvl": _TableSpec("observation_date"),
    "earnings_calendar": _TableSpec("report_date", "ticker"),
    "dividend_calendar": _TableSpec("ex_date", "ticker"),
    "economic_events": _TableSpec("event_time"),
    "corporate_filings": _TableSpec("filing_date", "ticker"),
    "insider_transactions": _TableSpec("transaction_date", "ticker"),
    "shares_outstanding": _TableSpec("date", "ticker"),
    "market_cap_history": _TableSpec("date", "ticker"),
    "income_statement": _TableSpec("period_end", "ticker", "income_statement_versions"),
    "balance_sheet": _TableSpec("period_end", "ticker", "balance_sheet_versions"),
    "cash_flow_statement": _TableSpec("period_end", "ticker", "cash_flow_statement_versions"),
    "fund_holdings": _TableSpec("as_of", None, "fund_holdings_versions"),
}

#: Bookkeeping columns left out of a table's hash: a re-fetch of the same
#: values rewrites them without changing the data.
_BOOKKEEPING_COLUMNS = frozenset({"updated_at", "ingested_at", "fetched_at", "created_at"})


def strategy_data_tables(strategy: Any, params: Mapping[str, Any] | None = None) -> tuple[str, ...]:
    """The lake tables ``strategy`` (a class or an instance) declares in
    ``data_tables``, plus those of the inner strategy of a wrapper
    (``inner_class_path`` in ``params``, else the wrapper's default),
    sorted. Wrappers need no change for this: the inner class is read from
    the params the wrapper is built with."""
    found: set[str] = set()
    _collect_tables(strategy, dict(params or {}), found, depth=0)
    return tuple(sorted(found))


def _collect_tables(strategy: Any, params: dict[str, Any], found: set[str], depth: int) -> None:
    if strategy is None or depth > 8:
        return
    found.update(str(t) for t in (getattr(strategy, "data_tables", ()) or ()))
    if hasattr(strategy, "params") and not isinstance(strategy, type):
        params = {**dict(getattr(strategy, "params", None) or {}), **params}
    defaults: dict[str, Any] = {}
    if callable(getattr(strategy, "parameter_spec", None)):
        try:
            defaults = {s.name: s.default for s in strategy.parameter_spec()}
        except Exception:  # an odd strategy: only its own tables count
            defaults = {}
    inner_path = params.get("inner_class_path", defaults.get("inner_class_path"))
    if not inner_path:
        return
    try:
        from stonks.strategies._wrapping import import_strategy_class

        inner = import_strategy_class(inner_path)
    except ValueError:
        return
    inner_params = params.get("inner_params", defaults.get("inner_params")) or {}
    _collect_tables(inner, dict(inner_params), found, depth + 1)


def data_fingerprint(dataset: Any, tables: Iterable[str] = ()) -> dict[str, Any]:
    """Per-ticker identity of the data a lab run reads, from a few queries.

    Bars: every OHLCV column at the dataset's interval, from the first
    stored bar (all look-back history) to the window end, end day
    inclusive. Corporate actions: every ``stock_splits`` and ``dividends``
    row up to the window end (they change adjusted prices, fills and cash).
    So a corrected open, a new split or a changed bar years before the
    start changes the hash (RS-18, P46). Tickers with no bars get
    ``count = 0``.

    ``tables`` (from :func:`strategy_data_tables`) are the other lake
    tables the strategy reads (macro series, TVL, an earnings calendar,
    filings). Each gets one hash under ``tables`` over its rows up to the
    window end (only the run's tickers for a per-ticker table), and over
    every row of its ``*_versions`` table with ``known_at`` where one
    exists. So a vendor revision changes the hash and names the table
    (P12, P46).

    ``tickers`` covers the universe. ``references`` covers every other
    ticker the run reads: each strategy's reference tickers (a regime
    filter's index, a reference market) and the benchmark. An intraday run
    also reads daily bars (regime filters, daily features), so ``daily``
    hashes them for every ticker (BE-28).

    ``statements`` hashes every stored version of the universe's income
    statement, balance sheet and cash flow rows with a period end up to the
    window end, ``known_at`` included (migration 018). A vendor restatement
    appends a version, so it changes this part even when the point-in-time
    read at each decision would not see it (roadmap 23.9)."""
    from stonks.lab.dataset import data_tickers

    start, end = dataset.full_window
    universe = sorted(set(dataset.universe))
    references = sorted(set(data_tickers(dataset)) - set(universe))
    return _fingerprint(
        dataset.lake,
        universe,
        references,
        _interval_code(dataset),
        start,
        end,
        statements=True,
        warmup_days=None,
        tables=sorted(set(tables)),
    )


def refingerprint(lake: Any, stored: dict[str, Any]) -> dict[str, Any]:
    """A stored :func:`data_fingerprint` recomputed on ``lake`` today: the
    same tickers, references, window, interval, warm-up and tables, so the
    two hashes match exactly when no bar, corporate action, statement
    version or table row they cover changed (roadmap 23.9, ``stonks lab
    verify``). A fingerprint stored before statements or tables were hashed
    is recomputed without them, and one made with a fixed warm-up keeps it."""
    start, end = (date.fromisoformat(str(d)[:10]) for d in stored["window"])
    warmup = stored.get("warmup_days", FINGERPRINT_WARMUP_DAYS)
    return _fingerprint(
        lake,
        sorted(stored.get("tickers") or {}),
        sorted(stored.get("references") or {}),
        str(stored.get("interval") or "1d"),
        start,
        end,
        statements="statements" in stored,
        warmup_days=None if warmup is None else int(warmup),
        tables=sorted(stored.get("tables") or {}),
    )


def fingerprint_changes(stored: dict[str, Any], current: dict[str, Any]) -> list[str]:
    """Tickers whose bars, corporate actions or statement versions differ
    between two fingerprints of the same data, sorted."""
    changed: set[str] = set()
    for part in ("tickers", "references", "daily", "statements"):
        changed.update(_part_changes(stored, current, part))
    return sorted(changed)


def changed_tables(stored: dict[str, Any], current: dict[str, Any]) -> list[str]:
    """Declared lake tables (``data_tables``) whose rows differ between two
    fingerprints (a revised macro print, a new TVL day), sorted."""
    return sorted(_part_changes(stored, current, "tables"))


def restated_tickers(stored: dict[str, Any], current: dict[str, Any]) -> list[str]:
    """Tickers whose statement versions differ between two fingerprints (a
    restatement, or a period filed since), sorted."""
    return sorted(_part_changes(stored, current, "statements"))


def _part_changes(stored: dict[str, Any], current: dict[str, Any], part: str) -> set[str]:
    before, after = stored.get(part) or {}, current.get(part) or {}
    return {t for t in set(before) | set(after) if before.get(t) != after.get(t)}


def _fingerprint(
    lake: Any,
    universe: list[str],
    references: list[str],
    interval: str,
    start: Any,
    end: Any,
    *,
    statements: bool = False,
    warmup_days: int | None = FINGERPRINT_WARMUP_DAYS,
    tables: list[str] | None = None,
) -> dict[str, Any]:
    everyone = universe + references
    stop = _as_date(end) + timedelta(days=1)
    bars = _bars_hashes(lake, everyone, interval, _as_date(start), stop, warmup_days)
    actions = _actions_hashes(lake, everyone, stop)

    def entries(names: list[str], hashes: dict[str, dict[str, Any]]) -> dict[str, Any]:
        out: dict[str, dict[str, Any]] = {}
        for t in names:
            out[t] = dict(hashes.get(t, _NO_BARS))
            if t in actions:
                out[t]["actions_hash"] = actions[t]
        return out

    body: dict[str, Any] = {
        "window": [str(start), str(end)],
        "warmup_days": warmup_days,
        "interval": interval,
        "tickers": entries(universe, bars),
        "references": entries(references, bars),
    }
    if _is_intraday(interval):
        daily = _bars_hashes(lake, everyone, "1d", _as_date(start), stop, warmup_days)
        body["daily"] = {t: dict(daily.get(t, _NO_BARS)) for t in everyone}
    if statements:
        body["statements"] = _statement_hashes(lake, universe, _as_date(end))
    if tables:
        body["tables"] = {t: _table_hash(lake, t, everyone, stop) for t in tables}
    digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
    return {**body, "hash": digest}


_NO_BARS: dict[str, Any] = {"count": 0, "first": None, "last": None, "bars_hash": None}


def _is_intraday(code: str) -> bool:
    from stonks.core.interval import Interval

    try:
        return Interval.parse(code).is_intraday
    except ValueError:
        return False


def _bars_hashes(
    lake: Any,
    tickers: list[str],
    interval: str,
    start: date,
    stop: date,
    warmup_days: int | None = None,
) -> dict[str, dict[str, Any]]:
    """Per ticker with bars: count and first stamp in the window, last
    stamp, and a hash of every bar before ``stop``: from the first stored
    bar, or from ``warmup_days`` before ``start`` (older fingerprints)."""
    if not tickers:
        return {}
    row_sql = " || '|' || ".join(
        f"COALESCE(CAST({c} AS VARCHAR), '')" for c in ("timestamp", *_BAR_COLUMNS)
    )
    since, args = "", [start, start, tickers, interval]
    if warmup_days is not None:
        since = " AND timestamp >= ?"
        args.append(start - timedelta(days=warmup_days))
    df = lake.sql(
        f"""
        SELECT ticker,
               COUNT(*) FILTER (WHERE timestamp >= ?) AS n,
               MIN(timestamp) FILTER (WHERE timestamp >= ?) AS first_ts,
               MAX(timestamp) AS last_ts,
               md5(string_agg({row_sql}, ',' ORDER BY timestamp)) AS bars_hash
        FROM bars
        WHERE ticker = ANY(?) AND interval = ?{since} AND timestamp < ?
        GROUP BY ticker
        """,
        [*args, stop],
    )
    return {
        str(row.ticker): {
            "count": int(row.n),
            "first": _iso(row.first_ts),
            "last": _iso(row.last_ts),
            "bars_hash": str(row.bars_hash),
        }
        for row in df.itertuples(index=False)
    }


def _actions_hashes(lake: Any, universe: list[str], stop: date) -> dict[str, str]:
    """Per ticker, a hash of its split and dividend rows dated before
    ``stop`` (tickers with none are left out)."""
    if not universe:
        return {}
    df = lake.sql(
        """
        SELECT ticker, md5(string_agg(row, ',' ORDER BY row)) AS h FROM (
            SELECT ticker, 's|' || CAST(date AS VARCHAR) || '|' || CAST(ratio AS VARCHAR) AS row
              FROM stock_splits WHERE ticker = ANY(?) AND date < ?
            UNION ALL
            SELECT ticker, 'd|' || CAST(ex_date AS VARCHAR) || '|' || CAST(amount AS VARCHAR)
              FROM dividends WHERE ticker = ANY(?) AND ex_date < ?
        ) GROUP BY ticker
        """,
        [universe, stop, universe, stop],
    )
    return {str(r.ticker): str(r.h) for r in df.itertuples(index=False)}


#: The versioned statement tables (migration 018) the fingerprint covers.
_STATEMENT_VERSIONS = (
    "income_statement_versions",
    "balance_sheet_versions",
    "cash_flow_statement_versions",
)


def _statement_hashes(lake: Any, universe: list[str], end: date) -> dict[str, str]:
    """Per ticker, a hash of every statement version (all columns and
    ``known_at``) with a period end on or before ``end``. Tickers with none
    are left out."""
    if not universe:
        return {}
    parts = " UNION ALL ".join(
        f"SELECT ticker, '{i}|' || CAST(v AS VARCHAR) AS row FROM {table} v"
        " WHERE ticker = ANY(?) AND period_end <= ?"
        for i, table in enumerate(_STATEMENT_VERSIONS)
    )
    df = lake.sql(
        f"SELECT ticker, md5(string_agg(row, ',' ORDER BY row)) AS h FROM ({parts}) GROUP BY ticker",
        [arg for _ in _STATEMENT_VERSIONS for arg in (universe, end)],
    )
    return {str(r.ticker): str(r.h) for r in df.itertuples(index=False)}


def _table_hash(lake: Any, table: str, tickers: list[str], stop: date) -> str | None:
    """One hash over a declared table's rows dated before ``stop`` (only
    ``tickers`` for a per-ticker table), and over its ``*_versions`` rows
    the same way when it has one, bookkeeping columns left out. ``None``
    when the lake has no such table."""
    spec = DATA_TABLES.get(table, _TableSpec(None))
    parts: list[str] = []
    args: list[Any] = []
    for i, name in enumerate((table, spec.versions)):
        if name is None:
            continue
        columns = _table_columns(lake, name)
        if not columns:
            if i == 0:
                return None
            continue
        kept = [c for c in columns if c not in _BOOKKEEPING_COLUMNS]
        row = " || '|' || ".join(f"COALESCE(CAST({_quote(c)} AS VARCHAR), '')" for c in kept)
        clauses = []
        if spec.date_column is not None and spec.date_column in columns:
            clauses.append(f"{_quote(spec.date_column)} < ?")
            args.append(stop)
        if spec.ticker_column is not None and spec.ticker_column in columns:
            clauses.append(f"{_quote(spec.ticker_column)} = ANY(?)")
            args.append(tickers)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        parts.append(f"SELECT '{i}|' || {row} AS row FROM {_quote(name)}{where}")
    df = lake.sql(
        "SELECT COUNT(*) AS n, md5(COALESCE(string_agg(row, ',' ORDER BY row), '')) AS h"
        f" FROM ({' UNION ALL '.join(parts)})",
        args,
    )
    return f"{int(df['n'].iloc[0])}:{df['h'].iloc[0]}"


def _table_columns(lake: Any, table: str) -> list[str]:
    df = lake.sql(
        "SELECT column_name FROM information_schema.columns WHERE table_name = ?"
        " AND table_catalog = current_database() AND table_schema = current_schema()"
        " ORDER BY ordinal_position",
        [table],
    )
    return [str(c) for c in df["column_name"]]


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _safe_fingerprint(dataset: Any, tables: Iterable[str] = ()) -> dict[str, Any] | None:
    if getattr(dataset, "lake", None) is None:
        return None
    try:
        return data_fingerprint(dataset, tables)
    except Exception as exc:  # never block a lab run on the manifest
        _log.warning("lab.manifest.fingerprint_failed", error=str(exc))
        return None


def _interval_code(dataset: Any) -> str:
    interval = getattr(dataset, "interval", None)
    return str(getattr(interval, "code", interval or "1d"))


def _as_date(d: Any) -> date:
    return d.date() if isinstance(d, datetime) else d


def _iso(ts: Any) -> str | None:
    if ts is None:
        return None
    return ts.isoformat() if hasattr(ts, "isoformat") else str(ts)


# ---- seeds -------------------------------------------------------------------------


def collect_seeds(tuner: Any, tests: Iterable[Any]) -> dict[str, Any]:
    """Seeds of the tuner and of every seeded survival test, read from a
    public ``seed`` or a private ``_seed`` attribute, else from the
    ``seed`` of the test's options or config model."""
    seeds: dict[str, Any] = {"tuner": _seed_of(tuner), "tests": {}}
    for test in tests:
        seed = _seed_of(test)
        if seed is not None:
            seeds["tests"][str(getattr(test, "id", type(test).__name__))] = seed
    return seeds


#: Attributes that hold a test's options or config model (a ``seed`` field).
_OPTION_HOLDERS = ("options", "config", "_options", "_config", "_cfg")


def _seed_of(obj: Any) -> Any:
    for name in ("seed", "_seed"):
        value = getattr(obj, name, None)
        if value is not None and not callable(value):
            return value
    for name in _OPTION_HOLDERS:
        value = getattr(getattr(obj, name, None), "seed", None)
        if value is not None and not callable(value):
            return value
    inner = getattr(obj, "_inner", None)  # wrappers such as cancellable tests
    return _seed_of(inner) if inner is not None and inner is not obj else None


# ---- recipe -------------------------------------------------------------------------


#: Attributes left out of a tuner's or test's options: the worker pool and
#: live RNG state change no result, and the seed is recorded apart.
_NOT_OPTIONS = frozenset({"seed", "parallel", "rng", "bound", "tuning", "inner", "max_workers"})


def build_recipe(
    tuner: Any,
    objective: Any,
    tests: Iterable[Any],
    *,
    budget: int | None = None,
    heatmap: Any = None,
    fixed_params: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """What the search was, so a run can be rerun from its manifest: the
    survival suite (each test id with its options), the tuner (class, seed
    and options), the objective, the budget, the heatmap options and the
    pinned params (P46)."""
    return {
        "suite": [
            {"id": str(getattr(t, "id", type(t).__name__)), "options": _options_of(t)}
            for t in tests
        ],
        "tuner": {
            "class": f"{type(tuner).__module__}:{type(tuner).__name__}",
            "seed": _seed_of(tuner),
            "options": _options_of(tuner),
        },
        "objective": {
            "name": str(getattr(objective, "name", type(objective).__name__)),
            "options": _options_of(objective),
        },
        "budget": budget,
        "heatmap": _jsonable(heatmap),
        "fixed_params": _jsonable(dict(fixed_params or {})),
    }


def _options_of(obj: Any) -> dict[str, Any]:
    """An options or config model when the object has one, else its plain
    settings: public and private attributes holding scalars or models."""
    for name in _OPTION_HOLDERS:
        model = getattr(obj, name, None)
        if hasattr(model, "model_dump"):
            return _jsonable(model)
    inner = getattr(obj, "_inner", None)
    if inner is not None and inner is not obj:
        return _options_of(inner)
    out: dict[str, Any] = {}
    for key, value in sorted(vars(obj).items()) if hasattr(obj, "__dict__") else ():
        name = key.lstrip("_")
        if name in _NOT_OPTIONS:
            continue
        if value is None or isinstance(value, str | int | float | bool):
            out[name] = value
        elif hasattr(value, "model_dump"):
            out[name] = _jsonable(value)
    return out


def _jsonable(obj: Any) -> Any:
    if obj is None:
        return None
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        obj = dataclasses.asdict(obj)
    return json.loads(json.dumps(obj, default=str))
