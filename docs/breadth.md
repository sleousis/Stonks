# Market breadth

Breadth tells you how many stocks take part in the market's move. An index
can rise on a few giants while most stocks fall. The Today page has a
"Market breadth" card with each number and a plain sentence.

It is display only. Nothing trades on it and no rule reads it.

| Number | What it means |
|---|---|
| Up and down | Stocks whose close rose or fell against the day before |
| Above 50 and 200 day average | Share of stocks closing above the mean of their last 50 or 200 closes |
| New highs and lows | Stocks at a one year high or low close |
| Distribution days | Sessions in the last 25 where the index fell 0.2% or more on higher volume than the day before |

Five or more distribution days in 25 sessions often means big sellers are
active (William O'Neil).

## Where the numbers come from

- Daily bars in the lake, adjusted closes, no bar after the day measured.
- Every equity in the lake by default. Funds with a holdings list and the
  index are left out. Set `universe_id` to measure a stored universe.
- A stock needs 60 sessions before it counts for highs and lows, and 50 or
  200 for each average.

```toml
[breadth]
# universe_id = "sp500"
index = "SPY.US"
distribution_window = 25
distribution_drop = 0.002
```

## Reads

- REST: `GET /api/market/breadth` (any signed-in reader), with `as_of` for
  an earlier day.
- MCP: `get_market_breadth`.
