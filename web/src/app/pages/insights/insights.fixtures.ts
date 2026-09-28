import type {
  AgreementView,
  InsightsTotalsView,
  InsightsView,
  LookThroughView,
  Page,
  RiskPolicy,
  RiskSnapshotView,
  RiskSummaryView,
  SnapshotView,
} from '../../api/models';

export const INSIGHTS: InsightsView = {
  portfolio_id: 'pf_default',
  taken_at: '2026-09-25T21:00:00Z',
  source: 'tick',
  currency: 'USD',
  cash: 20_000,
  total_value: 100_000,
  total_value_base: 92_000,
  fx_missing: [],
  mwr: 0.085,
  net_flows: 5_000,
  monthly_returns: [
    { month: '2026-07', value: 0.012 },
    { month: '2026-08', value: -0.031 },
    { month: '2026-09', value: 0.004 },
  ],
  allocation: {
    asset_class: [
      { key: 'equity', value: 70_000, weight: 0.7, holdings: 2 },
      { key: 'crypto', value: 10_000, weight: 0.1, holdings: 1 },
      { key: 'cash', value: 20_000, weight: 0.2, holdings: 0 },
    ],
    sector: [
      { key: 'Technology', value: 60_000, weight: 0.6, holdings: 2 },
      { key: 'cash', value: 20_000, weight: 0.2, holdings: 0 },
    ],
    currency: [{ key: 'USD', value: 100_000, weight: 1, holdings: 3 }],
    ticker: [
      { key: 'AAPL.US', value: 40_000, weight: 0.4, holdings: 1 },
      { key: 'MSFT.US', value: 30_000, weight: 0.3, holdings: 1 },
      { key: 'BTC-USD.CC', value: 10_000, weight: 0.1, holdings: 1 },
    ],
  },
  exposure: {
    long_value: 80_000,
    short_value: 0,
    gross: 0.8,
    net: 0.8,
    beta: 1.12,
    beta_coverage: 0.9,
    benchmark: 'SPY.US',
  },
  pnl: [
    {
      period: '1d',
      start_day: '2026-09-24',
      end_day: '2026-09-25',
      start_value: 99_000,
      end_value: 100_000,
      change: 1_000,
      change_pct: 0.0101,
      twr: 0.0042,
    },
    {
      period: 'inception',
      start_day: null,
      end_day: '2026-09-25',
      start_value: null,
      end_value: 100_000,
      change: null,
      change_pct: null,
    },
  ],
  risk: {
    concentration: {
      holdings: 3,
      top_weight: 0.5,
      top5_weight: 1,
      hhi: 0.38,
      effective_holdings: 2.6,
      largest: 'AAPL.US',
    },
    holdings: {
      observations: 250,
      volatility: 0.22,
      var_95: -0.021,
      expected_shortfall_95: -0.03,
      max_drawdown: -0.18,
      current_drawdown: -0.04,
    },
    history: {
      observations: 40,
      volatility: 0.2,
      var_95: -0.02,
      expected_shortfall_95: -0.028,
      max_drawdown: -0.09,
      current_drawdown: -0.05,
    },
    returns_as_of: '2026-09-25',
  },
  notes: ['Beta covers 90% of the holdings.'],
  uncovered: [],
  unpriced: [],
};

export const AGREEMENT: AgreementView = {
  portfolio_id: 'pf_default',
  as_of: '2026-09-25',
  strategies: ['momentum-v3', 'value-v1'],
  skipped: [],
  holdings: [
    {
      symbol: 'AAPL.US',
      ticker: 'AAPL.US',
      side: 'long',
      agree: 1,
      disagree: 1,
      opinions: [
        {
          strategy_id: 'momentum-v3',
          stance: 'agree',
          expected_return: 0.031,
          reason: 'ranked 2nd of 40 today',
        },
        {
          strategy_id: 'value-v1',
          stance: 'disagree',
          expected_return: -0.01,
          reason: 'priced above fair value',
        },
      ],
    },
  ],
};

export const TOTALS: InsightsTotalsView = {
  portfolios: 4,
  owners: 3,
  cash: 50_000,
  total_value: 400_000,
  asset_class: [
    { key: 'equity', value: 300_000, weight: 0.75, holdings: 12 },
    { key: 'cash', value: 50_000, weight: 0.125, holdings: 0 },
  ],
  exposure: {
    long_value: 350_000,
    short_value: 0,
    gross: 0.875,
    net: 0.875,
    beta: null,
    beta_coverage: 0,
    benchmark: null,
  },
};

export const SNAPSHOTS: Page<SnapshotView> = {
  items: [
    {
      id: 7,
      tick_id: 't7',
      taken_at: '2026-09-25T21:00:00Z',
      cash: 20_000,
      positions: { 'AAPL.US': 100, 'MSFT.US': 70 },
      total_value: 100_000,
    },
  ],
  total: 30,
  limit: 20,
  offset: 0,
};

export const POLICY: RiskPolicy = {
  enabled: true,
  max_weight_per_ticker: 0.5,
  max_open_positions: 10,
  max_weight_per_asset_class: { crypto: 0.05 },
  rules: {
    gross_exposure: { max_gross: 1.5 },
    portfolio_vol: { vol_cap: 0.25 },
    circuit_breaker: { max_drawdown_halt: 0.2 },
  },
};

export function snapshot(over: Partial<RiskSnapshotView> = {}): RiskSnapshotView {
  return {
    portfolio_id: 'pf_default',
    strategy_id: null,
    tick_id: 't7',
    as_of: '2026-09-25',
    value: 100_000,
    pnl: 1_000,
    realized_return: 0.01,
    sigma: 0.012,
    var_95: 0.02,
    var_99: 0.028,
    es_95: 0.025,
    es_99: 0.033,
    violation_95: false,
    violation_99: false,
    violations_95: 3,
    violations_99: 1,
    violation_ratio_95: 1.2,
    violation_ratio_99: 0.9,
    kupiec_p_95: 0.61,
    kupiec_p_99: 0.8,
    ratio_out_of_band: false,
    window_days: 60,
    observations: 60,
    exposures: {},
    expected_ir: null,
    ir_long: null,
    ir_short: null,
    decayed: false,
    decay_days: null,
    decay_reason: null,
    ...over,
  };
}

export const LIVE: RiskSummaryView = {
  portfolio_id: 'pf_default',
  as_of: '2026-09-25',
  portfolio: snapshot(),
  strategies: [
    snapshot({
      strategy_id: 'momentum-v3',
      value: 60_000,
      decayed: true,
      decay_days: 12,
      decay_reason: 'live IR 0.1 is under half the backtest IR 0.9',
      expected_ir: 0.9,
      ir_short: 0.1,
    }),
  ],
};

export const RISK_HISTORY: Page<RiskSnapshotView> = {
  items: [snapshot(), snapshot({ as_of: '2026-09-24', var_95: 0.019, value: 99_000 })],
  total: 2,
  limit: 20,
  offset: 0,
};

/** A book holding AAPL and SPY, with SPY split into what it owns (23.14). */
export const LOOK_THROUGH: LookThroughView = {
  portfolio_id: 'pf_1',
  as_of: '2026-09-28',
  currency: 'USD',
  total_value: 10_000,
  notes: ['90% of your fund value is in holdings the lists leave out (shown as not listed)'],
  look_through: {
    fund_value: 5_000,
    listed_fund_value: 500,
    funds: [
      {
        fund: 'SPY.US',
        as_of: '2026-09-25',
        source: 'eodhd',
        value: 5_000,
        holdings: 2,
        covered: 0.1,
      },
    ],
    names: [
      {
        key: 'AAPL.US',
        name: 'Apple Inc',
        value: 2_350,
        weight: 0.235,
        direct_value: 2_000,
        fund_value: 350,
        funds: ['SPY.US'],
      },
      {
        key: 'JPM.US',
        name: null,
        value: 150,
        weight: 0.015,
        direct_value: 0,
        fund_value: 150,
        funds: ['SPY.US'],
      },
    ],
    sector: [
      { key: 'not listed', value: 4_500, weight: 0.45, direct_value: 0, fund_value: 4_500 },
      { key: 'Technology', value: 2_350, weight: 0.235, direct_value: 2_000, fund_value: 350 },
      { key: 'cash', value: 3_000, weight: 0.3, direct_value: 3_000, fund_value: 0 },
    ],
    country: [{ key: 'US', value: 2_500, weight: 0.25, direct_value: 2_000, fund_value: 500 }],
  },
};
