# Capacity and cost

How big a server Stonks needs, what it costs, and where it stops scaling. Roadmap 14.10. Setup is in [deploy.md](deploy.md).

Every number below was measured on one machine with `tools/benchmark.py`. VM sizes and prices are **estimates** built from those numbers. Check them on your own data before you buy.

## How it was measured

All runs use seeded, synthetic data in a temp folder. Nothing touches the network.

```bash
uv run python -m tools.benchmark storage --tickers 200 --years 10
uv run python -m tools.benchmark tick                 # 1, 5, 20 strategies, then 5 and 20 portfolios
uv run python -m tools.benchmark lab --workers 1      # quick, standard, promotion
uv run --with psutil python -m tools.benchmark api --seconds 15
uv run python -m tools.benchmark all --out benchmark.json   # everything
```

- **Storage**: random-walk daily bars for N tickers over Y years, stored in the DuckDB bar table, then moved to Parquet. Bytes divided by ticker-years.
- **Tick**: 30 tickers, 320 days of bars, Momentum strategies (active, followed by `pf_default` in paper mode) and extra paper portfolios that follow every strategy. Four real ticks on four days after one warm-up tick. Median time, and state DB growth per tick.
- **Lab**: Momentum on 10 tickers over 500 days, random tuner with 10 trials, one run per survival preset.
- **API**: the seeded e2e stack (`tests/e2e/stack.py`) runs `stonks serve`. `tools/api_load.py` sends 4 clients for 15 s, each reading health, portfolio, strategies, orders, ticks and jobs with 0.1 s between requests. Three rounds: idle, while a lab run runs inside the API, and while the same run runs on a lab worker (roadmap 14.9). Memory is the resident size of the process tree.

Machine: AMD Ryzen 9 9950X (16 cores, 32 threads), 31 GB RAM, Windows 11, Python 3.13. A small cloud vCPU is slower, often by 2x or more, so the sizes below keep a wide margin.

## What we measured

### Storage

| Store | Bytes per ticker-year of daily bars |
|-------|-------------------------------------|
| Parquet bar store | about 12 KB |
| DuckDB bar table | about 23 KB |
| Empty migrated lake | 2 MB once |

So 5,000 tickers over 20 years (100,000 ticker-years) take about 1.2 GB in Parquet or 2.3 GB in DuckDB. Hourly bars have about 7 rows per trading day, so expect about 7 times more per ticker-year (estimate).

The state DB grows by 2 to 4 KB per strategy per tick, plus about 2 KB per extra portfolio. 20 portfolios following 5 strategies added about 70 KB per tick, under 20 MB a year.

### Tick

| Active strategies | Portfolios | Seconds per tick |
|-------------------|------------|------------------|
| 1 | 1 | 0.16 |
| 5 | 1 | 0.57 |
| 20 | 1 | 2.13 |
| 5 | 5 | 0.76 |
| 5 | 20 | 1.33 |

About 0.1 s per strategy on 30 tickers, and about 0.04 s per extra portfolio. Strategy time grows with the universe, so a 500-ticker universe costs roughly 1.5 to 2 s per strategy (estimate, linear). The tick runs once a day, so it is never the limit.

### Lab

| Preset | Survival tests | Seconds (1 process) |
|--------|----------------|---------------------|
| quick | 2 | 1.4 |
| standard | 7 | 8.9 |
| promotion | 14 | 43.7 |

Run time grows with tickers, bars and trials. A promotion run on 100 tickers over 5 years with 50 trials is more than 100 times the work of the row above, over an hour on one core (estimate). With 4 processes these small runs were no faster, because starting the pool costs as much as it saves. The pool pays off once one trial takes seconds.

### Memory and API latency

| Process | Resident memory |
|---------|-----------------|
| API (`stonks serve`), idle | 245 MB |
| API after the load test | 250 MB |
| API with a lab run inside | 293 MB |
| Lab worker with a lab run | 258 MB |

| Round (4 clients) | p50 | p95 | p99 | Requests/s |
|-------------------|-----|-----|-----|------------|
| Idle | 15 ms | 35 ms | 51 ms | 34 |
| Lab run inside the API | 24 ms | 50 ms | 54 ms | 32 |
| Same run on a lab worker | 10 ms | 32 ms | 34 ms | 35 |

A lab run inside the API made the median read 60 % slower. On the worker the API answered as fast as idle. This run used one process. A run that uses every core hurts more, which is why the worker has its own CPU limit and a lower CPU weight.

## Recommended sizes

Estimates. Prices are rough monthly figures from 2026 list prices and change often.

| Traders | VM | Lab | Disk | About per month |
|---------|----|-----|------|-----------------|
| 1 | 2 vCPU, 4 GB (Hetzner CX22 class) | in the API, or a worker with 1 CPU and 2 GB | 20 GB volume | 5 to 10 EUR (DigitalOcean about 24 USD) |
| 5 | 4 vCPU, 8 GB (Hetzner CX32 class) | worker with 2 CPUs and 3 GB | 40 GB volume | 10 to 15 EUR (DigitalOcean about 48 USD) |
| 20 | 8 vCPU, 16 GB (Hetzner CX42 class) | 1 or 2 workers with 4 CPUs and 6 GB each | 80 GB volume | 20 to 35 EUR (DigitalOcean about 96 USD) |

Add to each row:

- object storage for backups: 0 to 2 EUR
- Tailscale: free for a few users, paid seats past that
- the data plan (see limits below): 0 to about 100 USD

Why these sizes:

- The API needs about 250 MB plus about 50 MB per running job (`[api].max_concurrent_jobs`, default 2). The scheduler and Caddy add a few hundred MB. A worker needs about 250 MB plus its pool processes.
- The tick is a few seconds a day even at 20 traders. What grows with traders is lab work, and that goes to the worker.
- Keep the disk under 70 % full. Backups snapshot the data, and with the DuckDB bar table each lab snapshot copies the whole lake (two are kept). Use the Parquet bar store before you turn the worker on: its snapshots are hard links.
- For a big search, resize the VM for an hour rather than buying for the peak.

### Add-ons

Optional Compose profiles need memory on top of the rows above ([deploy.md](deploy.md#profiles)).

| Add-on | Profile | Memory |
|--------|---------|--------|
| IB Gateway, paper | `ibkr-paper` | about 1 GB (a Java process, 1 GB limit) |
| IB Gateway, live | `ibkr-live` | about 1 GB |
| AI model server (Ollama) | `ai` | about 6 GB for a 7 to 8B model at 4 bit, 8 GB limit by default |

- The 1-trader 4 GB VM holds the core plus one gateway.
- Paper and live gateways at once want the 8 GB size.
- The model server does not fit a small VM. Run it on a home server with 16 to 32 GB RAM or a GPU, or point `STONKS_AI_BASE_URL` at a model endpoint elsewhere.
- A home mini PC with 16 GB RAM (Intel N100 class) holds the core, both gateways and a lab worker.

## Limits

- **DuckDB has one writer.** Only the API process opens the lake read-write. Ingests and data fetches share one lane and run one at a time. Other processes read copies: the lab worker reads a snapshot, and CLI writes go through the API. More API processes would need a different lake, so grow the VM before adding processes.
- **SQLite in WAL mode** has one writer at a time and many readers. Writes wait up to 10 s for the lock. That is plenty for 20 traders, but the state DB must sit on a local disk, so the lab worker runs on the same host as the API. A worker on another machine needs a queue over the API, which is not built yet.
- **One API process.** `stonks serve` runs one process with a small job pool. Heavy reads from many clients at once queue behind each other. Measure with `tools/api_load.py --clients 20` before you grow past 20 traders.
- **The EODHD plan.** The free tier gives end-of-day prices only, for one year, with a small daily call limit. A paid end-of-day plan (about 20 USD a month) covers daily prices for a few thousand tickers. Fundamentals need a more expensive plan (about 60 to 100 USD a month), and each fundamentals call counts as several calls. Check the current plan page.
- **Disk alerts.** `deploy/monitor/check-host.sh` warns when the disk passes 80 % (roadmap 14.6).
