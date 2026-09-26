"""Move a lake's bars between the DuckDB table and the Parquet store.

    uv run python -m stonks.store.bars_migrate                 # to [lake.bars].backend
    uv run python -m stonks.store.bars_migrate --to parquet
    uv run python -m stonks.store.bars_migrate --lake data/lake.duckdb --to duckdb

The target defaults to ``[lake.bars] backend`` of the config
(``config/default.toml`` unless ``--config`` is given), the lake to
``[lake] path``. The copy is verified per (ticker, interval) by row count
and checksum before the lake switches; on a mismatch nothing changes.
Stop ``stonks serve`` first: the migration needs the lake's write lock.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from stonks.config import load_settings
from stonks.store.lake import DuckDBLake


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m stonks.store.bars_migrate", description=__doc__.split("\n\n")[0]
    )
    parser.add_argument("--config", type=Path, default=None, help="settings TOML file")
    parser.add_argument("--lake", type=Path, default=None, help="lake file (overrides config)")
    parser.add_argument(
        "--to", choices=("duckdb", "parquet"), default=None, help="target bar store"
    )
    args = parser.parse_args(argv)
    settings = load_settings(args.config) if args.config else load_settings()
    path = args.lake or settings.lake.path
    target = args.to or settings.lake.bars.backend
    with DuckDBLake(path) as lake:
        lake.migrate()
        if lake.bar_backend == target:
            print(f"{path}: bars already in {target}; nothing to do")
            return 0
        if target == "parquet":
            report = lake.migrate_bars_to_parquet()
        else:
            report = lake.migrate_bars_to_duckdb()
    print(
        f"{path}: moved {report.rows} bars ({report.series} series, {report.files} files) "
        f"to {report.backend} in {report.seconds}s; checksums verified"
    )
    if target == "duckdb":
        print(
            f"the Parquet files under {Path(path).parent / 'bars'} were kept; delete them by hand"
        )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
