import type {
  CalendarBucketView,
  GroupStatsView,
  JournalTradeDetailView,
  JournalTradeView,
  PnlCalendarView,
} from '../../api/models';

export function leg(over: Partial<JournalTradeView> = {}): JournalTradeView {
  return {
    leg_id: '7.1',
    trade_id: 7,
    ticker: 'AAPL.US',
    side: 'long',
    sleeve: 'manual',
    sleeve_name: null,
    origin: 'manual',
    entry_client_id: 'm1',
    exit_client_id: 'm2',
    entry_at: '2026-03-02T15:00:00Z',
    exit_at: '2026-03-05T15:00:00Z',
    quantity: 10,
    entry_price: 100,
    exit_price: 110,
    is_open: false,
    holding_days: 3,
    pnl: 100,
    return_pct: 0.1,
    fees: 0,
    dividends: 0,
    currency: 'USD',
    pnl_base: 100,
    mae_pct: -0.05,
    mfe_pct: 0.15,
    exit_efficiency: 0.75,
    exit_trigger: 'manual',
    stop_price: 95,
    stop_source: 'order_plan',
    target_price: null,
    risk_amount: 50,
    r_multiple: 2,
    mae_r: -1,
    tags: ['earnings'],
    mistakes: [],
    playbook_id: null,
    playbook_name: null,
    followed_plan: true,
    review: null,
    ...over,
  };
}

export function detail(over: Partial<JournalTradeDetailView> = {}): JournalTradeDetailView {
  return {
    trade_id: 7,
    portfolio_id: 'pf_default',
    ticker: 'AAPL.US',
    side: 'long',
    sleeve: 'manual',
    sleeve_name: null,
    origin: 'manual',
    legs: [leg()],
    pnl: 100,
    pnl_base: 100,
    tags: ['earnings'],
    mistakes: [],
    playbook_id: null,
    playbook_name: null,
    followed_plan: true,
    review: null,
    updated_by: null,
    updated_at: null,
    ...over,
  };
}

export function bucket(key: string, pnl: number, trades = 1): CalendarBucketView {
  return { key, start: key, end: key, pnl, trades, wins: pnl > 0 ? trades : 0 };
}

export function calendar(over: Partial<PnlCalendarView> = {}): PnlCalendarView {
  return {
    portfolio_id: 'pf_default',
    base_currency: 'USD',
    since: null,
    until: null,
    days: [bucket('2026-03-05', 100)],
    weeks: [bucket('2026-W10', 100)],
    months: [bucket('2026-03', 100)],
    total: 100,
    trades: 1,
    best_day: '2026-03-05',
    worst_day: '2026-03-05',
    unconverted: 0,
    fx_missing: [],
    ...over,
  };
}

export function group(over: Partial<GroupStatsView> = {}): GroupStatsView {
  return {
    key: 'broke',
    trades: 2,
    wins: 1,
    losses: 1,
    open: 0,
    win_rate: 0.5,
    pnl: 20,
    avg_pnl: 10,
    avg_win: 30,
    avg_loss: -10,
    profit_factor: 3,
    avg_r: 1.5,
    r_trades: 2,
    avg_holding_days: 2,
    avg_exit_efficiency: 0.6,
    ...over,
  };
}
