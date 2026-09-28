import type { EngineView, LatencyView, StreamStatusView } from '../../api/models';

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
      note: 'Intraday P&L per book arrives with live marks (roadmap 21.3.3).',
    },
    ...over,
  };
}
