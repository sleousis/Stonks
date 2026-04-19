# Block 5 — Production Tick

> Status: **implemented** for paper mode via `SimulatedBroker`. Live broker adapters (Alpaca, IB, etc.) remain a post-MVP drop-in behind the same `Broker` Protocol.

## Purpose

One fresh Python process per tick, invoked by an external scheduler. Pulls latest data, ranks active strategies × universe, executes the best candidates through the broker, records everything. Stateless across invocations — all state is in DuckDB + SQLite.

## Module layout (target)

```
src/stonks/production/
├── tick.py          # entrypoint: stonks tick
└── ranker.py        # strategies × universe → ordered (expected_return, strategy, ticker)
src/stonks/execution/
├── broker.py        # Broker Protocol
├── orders.py        # Order, Fill, client_id helper
└── brokers/         # SimulatedBroker (reused from backtest), AlpacaBroker, IbBroker, ...
```

## Tick flow

```mermaid
sequenceDiagram
    autonumber
    participant Cron
    participant Tick as stonks tick
    participant Reg as StrategyRegistry
    participant Lake as DuckDBLake
    participant Rank as Ranker
    participant Brk as Broker
    participant State as SqliteState

    Cron->>Tick: invoke (fresh process)
    Tick->>State: begin tick_run (id = ulid)
    Tick->>Reg: list_active()
    Reg-->>Tick: [StrategyHandle]
    Tick->>Lake: universe-of-the-day, latest prices
    Tick->>Rank: rank(strategies, tickers, as_of=today)
    loop per strategy in handles
        Rank->>Rank: features = strategy.extract_features(ticker, today, lake)
        Rank->>Rank: r_hat = strategy.estimate_return(...)
    end
    Rank-->>Tick: ordered [(r_hat, strategy, ticker)]
    Tick->>Brk: fetch_portfolio()
    loop per top candidate
        Tick->>Tick: orders = strategy.decide(ranked, portfolio)
        loop per order
            Tick->>Brk: place_order(order)     # idempotent via client_id
            Brk-->>Tick: Fill?
            Tick->>State: record order + fill
        end
    end
    Tick->>Brk: reconcile()
    Tick->>State: close tick_run (status, summary)
```

## `Broker` Protocol

```python
class Broker(Protocol):
    def fetch_portfolio(self) -> Portfolio: ...
    def place_order(self, order: Order) -> Fill | None: ...
    def reconcile(self) -> list[Fill]: ...
```

`place_order` must be idempotent: the broker keeps a record of `order.client_id` and re-submission of the same id returns the existing state, never places a duplicate. `SimulatedBroker` honors this too (trivial partial-tick recovery in both worlds).

## Idempotency + recovery

- Each tick gets a `tick_id` (ulid).
- `order.client_id = f"{tick_id}:{strategy_id}:{ticker}:{side}"`.
- If a tick crashes mid-way, the next tick re-derives the same client_ids; the broker short-circuits duplicates.
- `reconcile()` at end-of-tick checks broker truth vs `state.orders` / `state.fills`; mismatches flip `tick_runs.status = 'partial'` and emit an alert.

## CLI

```
stonks tick                       # one-shot; suitable for cron
stonks tick --dry-run             # rank and log intended orders without placing
stonks tick --strategy <id>       # restrict to a single strategy (debug)
```

## Scheduling

- `config/default.toml` suggests a default cron line; actually wiring cron/systemd is host-specific and out of scope for the repo.
- The tick does not enforce market hours; the scheduler is responsible (e.g., cron `0 15 * * 1-5`).

## Testing (target)

- `tests/integration/test_tick_dry_run.py` — fixture lake + registry with one fake strategy, assert ranked orders are logged without a broker call.
- `tests/integration/test_tick_idempotency.py` — run same tick twice with a `SimulatedBroker`; assert no duplicate orders.

## Milestones

- **Prod M1:** `SimulatedBroker`-backed paper tick (what `stonks tick --dry-run` effectively does plus local fill simulation).
- **Prod M2:** first live broker impl; pre-trade checks (position limits, cash caps); alerting on `tick_runs.status = 'partial'`.
