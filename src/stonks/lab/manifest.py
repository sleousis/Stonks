"""Reproducibility manifest (BL-06): what it takes to rerun a lab result.

:func:`build_manifest` is a pure builder the runner calls once per run. The
manifest is stored in ``lab_runs.manifest_json`` and in the artifact's
``meta.json``:

- ``git_sha`` / ``git_dirty`` (``None`` when git or a checkout is missing);
- ``stonks_version``, ``python_version`` and ``libraries`` (versions of the
  numeric stack);
- ``config_hash``: sha256 of the resolved settings JSON, secrets excluded;
- ``data_fingerprint``: per universe ticker, bar ``count``, ``first`` /
  ``last`` timestamp and an md5 of the ordered ``close|adj_close`` sequence
  over the lab window, from one DuckDB query, plus one sha256 over all of it;
- ``costs``: the cost-model settings in force;
- ``shorting``: the margin model and borrow fees, only for a dataset that
  may short (roadmap 16.4);
- ``dataset``: universe, windows and interval;
- ``seeds``.
"""

from __future__ import annotations

import hashlib
import json
import platform
import re
import subprocess
from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime, timedelta
from importlib import metadata
from pathlib import Path
from typing import Any

from stonks.logging import get_logger

_log = get_logger("stonks.lab.manifest")

#: Setting names whose values are never hashed (credentials, tokens).
_SECRET_KEY = re.compile(r"(key|secret|token|password|passphrase|credential)", re.IGNORECASE)
_LIBRARIES = ("numpy", "pandas", "scipy", "duckdb", "scikit-learn", "pydantic")
_REPO_DIR = Path(__file__).resolve().parent


def build_manifest(settings: Any, dataset: Any, seeds: Mapping[str, Any]) -> dict[str, Any]:
    """The run's manifest. Every part degrades to ``None`` instead of
    raising, so a manifest problem never blocks a lab run."""
    return {
        **git_info(),
        "stonks_version": _stonks_version(),
        "python_version": platform.python_version(),
        "libraries": library_versions(),
        "config_hash": config_hash(settings),
        "data_fingerprint": _safe_fingerprint(dataset),
        "costs": _costs(settings, dataset),
        "dataset": dataset_summary(dataset),
        "seeds": _jsonable(dict(seeds)),
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        **_shorting(dataset),
    }


def _shorting(dataset: Any) -> dict[str, Any]:
    shorting = getattr(dataset, "shorting", None)
    return {} if shorting is None else {"shorting": _jsonable(shorting)}


# ---- code identity -------------------------------------------------------------


def git_info(cwd: Path | None = None) -> dict[str, Any]:
    """``{"git_sha", "git_dirty"}`` of the checkout holding this package
    (or ``cwd``); both ``None`` when git is unavailable or it is not a repo."""
    where = str(cwd or _REPO_DIR)
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=where, capture_output=True, text=True, timeout=10
        )
        if sha.returncode != 0:
            return {"git_sha": None, "git_dirty": None}
        status = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            cwd=where,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return {"git_sha": None, "git_dirty": None}
    dirty = bool(status.stdout.strip()) if status.returncode == 0 else None
    return {"git_sha": sha.stdout.strip() or None, "git_dirty": dirty}


def library_versions() -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    for name in _LIBRARIES:
        try:
            out[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            out[name] = None
    return out


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


#: Calendar days of warm-up history before the window start that the data
#: fingerprint covers: about a year and a half, past the longest look-back
#: of the catalogued strategies (RS-18).
FINGERPRINT_WARMUP_DAYS = 550

_BAR_COLUMNS = ("open", "high", "low", "close", "adj_close", "volume")


def data_fingerprint(dataset: Any) -> dict[str, Any]:
    """Per-ticker identity of the data a lab run reads, from a few queries.

    Bars: every OHLCV column at the dataset's interval, from
    :data:`FINGERPRINT_WARMUP_DAYS` before the window start (look-back
    history) to the window end, end day inclusive. Corporate actions: every
    ``stock_splits`` and ``dividends`` row up to the window end (they change
    adjusted prices, fills and cash). So a corrected open, a new split or a
    changed warm-up bar changes the hash (RS-18, P46). Tickers with no bars
    get ``count = 0``.

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
    )


def refingerprint(lake: Any, stored: dict[str, Any]) -> dict[str, Any]:
    """A stored :func:`data_fingerprint` recomputed on ``lake`` today: the
    same tickers, references, window and interval, so the two hashes match
    exactly when no bar, corporate action or statement version they cover
    changed (roadmap 23.9, ``stonks lab verify``). A fingerprint stored
    before statements were hashed is recomputed without them."""
    start, end = (date.fromisoformat(str(d)[:10]) for d in stored["window"])
    return _fingerprint(
        lake,
        sorted(stored.get("tickers") or {}),
        sorted(stored.get("references") or {}),
        str(stored.get("interval") or "1d"),
        start,
        end,
        statements="statements" in stored,
    )


def fingerprint_changes(stored: dict[str, Any], current: dict[str, Any]) -> list[str]:
    """Tickers whose bars, corporate actions or statement versions differ
    between two fingerprints of the same data, sorted."""
    changed: set[str] = set()
    for part in ("tickers", "references", "daily", "statements"):
        changed.update(_part_changes(stored, current, part))
    return sorted(changed)


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
) -> dict[str, Any]:
    everyone = universe + references
    stop = _as_date(end) + timedelta(days=1)
    bars = _bars_hashes(lake, everyone, interval, _as_date(start), stop)
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
        "warmup_days": FINGERPRINT_WARMUP_DAYS,
        "interval": interval,
        "tickers": entries(universe, bars),
        "references": entries(references, bars),
    }
    if _is_intraday(interval):
        daily = _bars_hashes(lake, everyone, "1d", _as_date(start), stop)
        body["daily"] = {t: dict(daily.get(t, _NO_BARS)) for t in everyone}
    if statements:
        body["statements"] = _statement_hashes(lake, universe, _as_date(end))
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
    lake: Any, tickers: list[str], interval: str, start: date, stop: date
) -> dict[str, dict[str, Any]]:
    """Per ticker with bars: count and first stamp in the window, last
    stamp, and a hash of every bar from the warm-up start to ``stop``."""
    if not tickers:
        return {}
    first_day = start - timedelta(days=FINGERPRINT_WARMUP_DAYS)
    row_sql = " || '|' || ".join(
        f"COALESCE(CAST({c} AS VARCHAR), '')" for c in ("timestamp", *_BAR_COLUMNS)
    )
    df = lake.sql(
        f"""
        SELECT ticker,
               COUNT(*) FILTER (WHERE timestamp >= ?) AS n,
               MIN(timestamp) FILTER (WHERE timestamp >= ?) AS first_ts,
               MAX(timestamp) AS last_ts,
               md5(string_agg({row_sql}, ',' ORDER BY timestamp)) AS bars_hash
        FROM bars
        WHERE ticker = ANY(?) AND interval = ? AND timestamp >= ? AND timestamp < ?
        GROUP BY ticker
        """,
        [start, start, tickers, interval, first_day, stop],
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


def _safe_fingerprint(dataset: Any) -> dict[str, Any] | None:
    if getattr(dataset, "lake", None) is None:
        return None
    try:
        return data_fingerprint(dataset)
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
    public ``seed`` or a private ``_seed`` attribute."""
    seeds: dict[str, Any] = {"tuner": _seed_of(tuner), "tests": {}}
    for test in tests:
        seed = _seed_of(test)
        if seed is not None:
            seeds["tests"][str(getattr(test, "id", type(test).__name__))] = seed
    return seeds


def _seed_of(obj: Any) -> Any:
    for name in ("seed", "_seed"):
        value = getattr(obj, name, None)
        if value is not None and not callable(value):
            return value
    inner = getattr(obj, "_inner", None)  # wrappers such as cancellable tests
    return _seed_of(inner) if inner is not None and inner is not obj else None


def _jsonable(obj: Any) -> Any:
    if obj is None:
        return None
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    return json.loads(json.dumps(obj, default=str))
