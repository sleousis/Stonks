"""Split the test suite into CI shards.

CI runs pytest in several parallel jobs. Each job asks this tool for its
share of the test modules. The split is by file and deterministic: the
largest files go first, each to the shard with the least bytes so far. File
size stands in for run time, which keeps the shards roughly even without a
timing file to maintain. Every test module lands in exactly one shard.

    uv run python -m tools.ci_shards 1 4    # the files of shard 1 of 4
"""

from __future__ import annotations

import sys
from pathlib import Path

#: The module names pytest collects by default (``python_files``).
PATTERNS = ("test_*.py", "*_test.py")


def find_test_files(tests: Path, root: Path) -> list[str]:
    """Every test module under ``tests``, as sorted POSIX paths from ``root``."""
    found = {path for pattern in PATTERNS for path in tests.rglob(pattern) if path.is_file()}
    return sorted(path.relative_to(root).as_posix() for path in found)


def shard(tests: Path, root: Path, index: int, total: int) -> list[str]:
    """The test modules of shard ``index`` (1-based) out of ``total``."""
    if total < 1 or not 1 <= index <= total:
        raise ValueError(f"shard {index} of {total} is out of range")
    sizes = {name: (root / name).stat().st_size for name in find_test_files(tests, root)}
    loads = [0] * total
    shards: list[list[str]] = [[] for _ in range(total)]
    for name in sorted(sizes, key=lambda n: (-sizes[n], n)):
        target = loads.index(min(loads))
        shards[target].append(name)
        loads[target] += sizes[name]
    return sorted(shards[index - 1])


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    try:
        index, total = (int(a) for a in args)
        files = shard(Path("tests"), Path("."), index, total)
    except ValueError as exc:
        print(f"usage: python -m tools.ci_shards INDEX TOTAL ({exc})", file=sys.stderr)
        return 2
    print("\n".join(files))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
