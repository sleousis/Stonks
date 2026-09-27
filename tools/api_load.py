"""Light API load: a few clients read common console pages for a while and
report latency percentiles (roadmap 14.10, docs/capacity.md).

    uv run python -m tools.api_load --url http://127.0.0.1:8000 --token $STONKS_API_TOKEN
    uv run python -m tools.api_load --clients 4 --seconds 20 --json

Each client loops over :data:`DEFAULT_PATHS` with a short think time, like
a trader clicking around. Standard library only, so it runs anywhere.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass

#: Pages the console reads most: health, home, strategies, orders, ticks, jobs.
DEFAULT_PATHS: tuple[str, ...] = (
    "/api/health",
    "/api/portfolio",
    "/api/strategies?limit=50",
    "/api/orders?limit=50",
    "/api/ticks?limit=20",
    "/api/jobs?limit=20",
)


@dataclass(frozen=True)
class LoadResult:
    requests: int
    errors: int
    seconds: float
    requests_per_second: float
    p50_ms: float
    p95_ms: float
    p99_ms: float
    max_ms: float


def percentile(values: Sequence[float], q: float) -> float:
    """The ``q`` quantile (0..1) of ``values`` by linear interpolation."""
    if not values:
        return 0.0
    ordered = sorted(values)
    pos = (len(ordered) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


def summarize(latencies_ms: Sequence[float], errors: int, seconds: float) -> LoadResult:
    n = len(latencies_ms) + errors
    return LoadResult(
        requests=n,
        errors=errors,
        seconds=round(seconds, 2),
        requests_per_second=round(n / seconds, 1) if seconds > 0 else 0.0,
        p50_ms=round(percentile(latencies_ms, 0.50), 1),
        p95_ms=round(percentile(latencies_ms, 0.95), 1),
        p99_ms=round(percentile(latencies_ms, 0.99), 1),
        max_ms=round(max(latencies_ms, default=0.0), 1),
    )


def _fetcher(base_url: str, token: str | None, timeout: float) -> Callable[[str], None]:
    headers = {"Authorization": f"Bearer {token}"} if token else {}

    def fetch(path: str) -> None:
        req = urllib.request.Request(base_url.rstrip("/") + path, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read()

    return fetch


def run_load(
    base_url: str,
    *,
    token: str | None = None,
    clients: int = 4,
    seconds: float = 20.0,
    think_seconds: float = 0.1,
    paths: Sequence[str] = DEFAULT_PATHS,
    timeout: float = 30.0,
    fetch: Callable[[str], None] | None = None,
) -> LoadResult:
    """Run ``clients`` threads for ``seconds`` and summarize their latencies."""
    fetch = fetch or _fetcher(base_url, token, timeout)
    latencies: list[float] = []
    errors = [0]
    lock = threading.Lock()
    deadline = time.monotonic() + seconds

    def client(offset: int) -> None:
        i = offset
        while time.monotonic() < deadline:
            path = paths[i % len(paths)]
            i += 1
            started = time.perf_counter()
            try:
                fetch(path)
            except (urllib.error.URLError, OSError, ValueError):
                with lock:
                    errors[0] += 1
            else:
                with lock:
                    latencies.append((time.perf_counter() - started) * 1000.0)
            if think_seconds:
                time.sleep(think_seconds)

    started = time.monotonic()
    threads = [threading.Thread(target=client, args=(k,), daemon=True) for k in range(clients)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return summarize(latencies, errors[0], time.monotonic() - started)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tools.api_load", description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--token", default=None, help="API token (default: none)")
    parser.add_argument("--clients", type=int, default=4)
    parser.add_argument("--seconds", type=float, default=20.0)
    parser.add_argument("--think", type=float, default=0.1, help="pause between requests (s)")
    parser.add_argument("--json", action="store_true", help="print JSON")
    args = parser.parse_args(argv)
    result = run_load(
        args.url,
        token=args.token,
        clients=args.clients,
        seconds=args.seconds,
        think_seconds=args.think,
    )
    if args.json:
        print(json.dumps(asdict(result)))
    else:
        print(
            f"{result.requests} requests ({result.errors} errors) in {result.seconds} s, "
            f"{result.requests_per_second}/s; p50 {result.p50_ms} ms, p95 {result.p95_ms} ms, "
            f"p99 {result.p99_ms} ms, max {result.max_ms} ms"
        )
    return 0 if result.errors == 0 else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
