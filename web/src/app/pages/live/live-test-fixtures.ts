import type {
  EngineView,
  IntradaySnapshotView,
  LatencyView,
  StreamStatusView,
} from '../../api/models';

export function latency(over: Partial<LatencyView> = {}): LatencyView {
  return {
    count: 0,
    p50_seconds: null,
    p95_seconds: null,
    mean_seconds: null,
    max_seconds: null,
    ...over,
  };
}

export function engine(over: Partial<EngineView> = {}): EngineView {
  return {
    engine_id: 'intraday',
    calendar: 'XNYS',
    state: 'streaming',
    live: true,
    started_at: '2026-09-28T13:00:00Z',
    updated_at: '2026-09-28T15:00:00Z',
    stopped_at: null,
    last_dispatch_at: '2026-09-28T15:00:00Z',
    last_dispatch_age_seconds: 20,
    market_open: true,
    deadman: 'ok',
    silent_seconds: null,
    bar_closes: 90,
    bars: 180,
    late_bars: 1,
    pending_closes: 0,
    handler_errors: {},
    stream: {
      source: 'eodhd',
      state: 'streaming',
      connected: true,
      connected_at: '2026-09-28T13:00:00Z',
      last_event_at: '2026-09-28T15:00:10Z',
      last_event_age_seconds: 3,
      connects: 1,
      disconnects: 0,
      bars_written: 175,
      late_ticks: 4,
      write_errors: 0,
      gaps: 1,
      backfills_ok: 1,
      backfills_failed: 0,
      last_error: null,
    },
    dispatch_lag: latency({ count: 90, p50_seconds: 0.05, p95_seconds: 0.25, max_seconds: 0.4 }),
    event_to_order: latency({ count: 6, p50_seconds: 0.5, p95_seconds: 1, max_seconds: 0.9 }),
    ...over,
  };
}

export function status(over: Partial<StreamStatusView> = {}): StreamStatusView {
  return {
    as_of: '2026-09-28T15:00:30Z',
    streaming_enabled: true,
    source: 'eodhd',
    deadman_minutes: 5,
    stale_after_seconds: 120,
    engines: [engine()],
    intraday_pnl: {
      available: false,
      snapshot_minutes: 5,
      note: 'Intraday P&L is not kept on this server, so there is nothing to show here.',
    },
    ...over,
  };
}

/** A status whose intraday P&L rows are kept. */
export function statusWithPnl(): StreamStatusView {
  return status({
    intraday_pnl: {
      available: true,
      snapshot_minutes: 5,
      note: 'Intraday P&L per portfolio from the engine live marks, stored every 5 minutes.',
    },
  });
}

export function pnlRow(over: Partial<IntradaySnapshotView> = {}): IntradaySnapshotView {
  return {
    portfolio_id: 'pf_default',
    strategy_id: null,
    day: '2026-09-28',
    at: '2026-09-28T15:00:00Z',
    start_value: 100_000,
    value: 101_250,
    realised: 500,
    unrealised: 800,
    fees: 50,
    pnl: 1_250,
    day_return: 0.0125,
    high_water_pnl: 1_500,
    drawdown: -0.0025,
    gross_exposure: 40_000,
    net_exposure: 40_000,
    exposures: { 'AAPL.US': 40_000 },
    fills: 6,
    unmarked: 0,
    stale_marks: 0,
    max_mark_age_seconds: 30,
    ...over,
  };
}
