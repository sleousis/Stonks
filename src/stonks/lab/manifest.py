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
    }


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
        return summary
    except Exception:
        return None


def data_fingerprint(dataset: Any) -> dict[str, Any]:
    """Per-ticker identity of the bars a lab run reads, from one query.

    Window is ``dataset.full_window`` with the end day inclusive, at the
    dataset's interval. Tickers with no bars get ``count = 0``."""
    start, end = dataset.full_window
    interval = _interval_code(dataset)
    universe = sorted(set(dataset.universe))
    df = dataset.lake.sql(
        """
        SELECT ticker,
               COUNT(*) AS n,
               MIN(timestamp) AS first_ts,
               MAX(timestamp) AS last_ts,
               md5(string_agg(
                   CAST(close AS VARCHAR) || '|' || COALESCE(CAST(adj_close AS VARCHAR), ''),
                   ',' ORDER BY timestamp
               )) AS close_hash
        FROM bars
        WHERE ticker = ANY(?) AND interval = ? AND timestamp >= ? AND timestamp < ?
        GROUP BY ticker
        """,
        [universe, interval, _as_date(start), _as_date(end) + timedelta(days=1)],
    )
    found = {row.ticker: row for row in df.itertuples(index=False)}
    tickers: dict[str, dict[str, Any]] = {}
    for t in universe:
        row = found.get(t)
        tickers[t] = (
            {
                "count": int(row.n),
                "first": _iso(row.first_ts),
                "last": _iso(row.last_ts),
                "close_hash": str(row.close_hash),
            }
            if row is not None
            else {"count": 0, "first": None, "last": None, "close_hash": None}
        )
    body = {"window": [str(start), str(end)], "interval": interval, "tickers": tickers}
    digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
    return {**body, "hash": digest}


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
