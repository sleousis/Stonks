import type {
  Job,
  OptionChainView,
  OptionPayoffView,
  OptionStrategyView,
  OptionStructureView,
  OptionUnderlyingView,
  OptionsBacktestView,
} from '../../api/models';

/** Test data for the options page's specs. */

export const UNDERLYING: OptionUnderlyingView = {
  underlying: 'AAPL.US',
  first_day: '2026-01-02',
  last_day: '2026-03-31',
  days: 61,
  contracts: 126,
  sources: ['synthetic'],
  synthetic: true,
};

export const CHAIN: OptionChainView = {
  underlying: 'AAPL.US',
  as_of: '2026-03-31',
  spot: 101.25,
  expiries: ['2026-04-17', '2026-05-15'],
  expiry: '2026-04-17',
  days_to_expiry: 17,
  models: ['american_baw'],
  synthetic: true,
  rows: [
    {
      strike: 100,
      call: {
        contract_id: 'AAPL.US:2026-04-17:C:100',
        bid: 2.9,
        ask: 3.1,
        mark: 3.0,
        volume: 100,
        open_interest: 1000,
        iv: 0.25,
        delta: 0.56,
        gamma: 0.061,
        theta: -0.052,
        vega: 0.112,
      },
      put: {
        contract_id: 'AAPL.US:2026-04-17:P:100',
        bid: 1.6,
        ask: 1.8,
        mark: 1.7,
        volume: 100,
        open_interest: 900,
        iv: 0.26,
        delta: -0.44,
        gamma: 0.061,
        theta: -0.048,
        vega: 0.112,
      },
    },
    {
      strike: 105,
      call: {
        contract_id: 'AAPL.US:2026-04-17:C:105',
        bid: 1.0,
        ask: 1.1,
        mark: 1.05,
        iv: 0.24,
        delta: 0.28,
        gamma: 0.05,
        theta: -0.04,
        vega: 0.09,
      },
      put: null,
    },
  ],
};

export const STRUCTURES: OptionStructureView[] = [
  { name: 'bull_call_spread', params: ['dte', 'long_delta', 'short_delta'], holds_shares: false },
  { name: 'covered_call', params: ['dte', 'delta'], holds_shares: true },
];

export const STRATEGIES: OptionStrategyView[] = [
  {
    id: 'cash_secured_put',
    hypothesis: 'Selling puts on names we want to own earns the volatility premium.',
    structures: ['cash_secured_put'],
    parameters: [
      {
        name: 'delta',
        kind: 'float',
        default: 0.3,
        bounds: [0.1, 0.5],
        tunable: true,
        description: 'target delta',
      },
    ],
  },
  {
    id: 'vertical_spread',
    hypothesis: 'Momentum picks the direction and a spread caps the loss.',
    structures: ['bull_call_spread', 'bear_put_spread'],
    parameters: [],
  },
];

export const PAYOFF: OptionPayoffView = {
  underlying: 'AAPL.US',
  as_of: '2026-03-31',
  spot: 100,
  structure: 'bull_call_spread',
  legs: [
    {
      instrument: 'AAPL.US:2026-04-17:C:100',
      kind: 'option',
      right: 'call',
      strike: 100,
      expiry: '2026-04-17',
      quantity: 1,
      price: 3,
    },
    {
      instrument: 'AAPL.US:2026-04-17:C:110',
      kind: 'option',
      right: 'call',
      strike: 110,
      expiry: '2026-04-17',
      quantity: -1,
      price: 1,
    },
  ],
  points: [
    { spot: 70, profit: -200 },
    { spot: 100, profit: -200 },
    { spot: 102, profit: 0 },
    { spot: 110, profit: 800 },
    { spot: 130, profit: 800 },
  ],
  cost: 200,
  max_loss: 200,
  max_gain: 800,
  breakevens: [102],
  synthetic: true,
};

export const BACKTEST: OptionsBacktestView = {
  strategy: 'cash_secured_put',
  underlyings: ['AAPL.US'],
  start: '2026-01-02',
  end: '2026-03-31',
  days: 3,
  final_return: 0.012,
  sharpe: null,
  max_drawdown: -0.004,
  cagr: 0.05,
  fills: 4,
  rejected: 1,
  rejection_reasons: { 'no two-sided quote': 1 },
  equity: [
    { date: '2026-01-02', value: 100000 },
    { date: '2026-01-05', value: 100400 },
    { date: '2026-01-06', value: 101200 },
  ],
  sources: ['synthetic'],
  synthetic: true,
  validation: [
    { test_id: 'oos', passed: false, metrics: { psr: 0.4 }, notes: '' },
    { test_id: 'cost_stress', passed: true, metrics: { sharpe_per_bar: 0.1 }, notes: '' },
  ],
  verdict: 'failed',
};

export function job(id: string, patch: Partial<Job> = {}): Job {
  return {
    id,
    kind: 'options_backtest',
    status: 'succeeded',
    progress: 1,
    created_at: '2026-09-27T10:00:00Z',
    params: {},
    ...patch,
  };
}
