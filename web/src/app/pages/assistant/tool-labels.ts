/**
 * Plain names for the tools the assistant may use (its catalog in the
 * backend), so a step reads "Read your portfolio", not a tool id. A tool
 * missing here falls back to its id in words ("get_thing" reads "Get thing").
 */
const TOOL_LABELS: Readonly<Record<string, string>> = {
  whoami: 'Check who you are',
  list_tool_categories: 'Look at what else it can use',
  enable_tool_category: 'Turn on more tools',
  // Portfolio
  get_portfolio: 'Read your portfolio',
  list_portfolios: 'List your portfolios',
  get_pnl: 'Read your profit and loss',
  get_insights: 'Read portfolio insights',
  list_portfolio_snapshots: 'Read portfolio history',
  get_strategy_agreement: 'Check which strategies agree',
  list_trading_modes: 'Read trading modes',
  get_tca_summary: 'Read trade costs',
  list_trade_journal: 'Read the trade journal',
  get_order_tca: 'Read the costs of an order',
  list_cash_flows: 'Read deposits and withdrawals',
  get_tax_settings: 'Read tax settings',
  get_fx_rate: 'Look up an exchange rate',
  list_orders: 'List orders',
  list_fills: 'List fills',
  // Market
  search_instruments: 'Search tickers',
  get_bars: 'Read prices',
  get_coverage: 'Check data coverage',
  get_catalog: 'Read the data catalog',
  get_chart: 'Read a chart',
  list_watchlists: 'List your watchlists',
  get_watchlist: 'Read a watchlist',
  list_sources: 'List data providers',
  // Strategies
  list_strategies: 'List strategies',
  get_strategy: 'Read a strategy',
  get_strategy_history: 'Read strategy history',
  get_golive_report: 'Read the go-live checks',
  get_leaderboard: 'Read the leaderboard',
  get_tear_sheet: 'Read a tear sheet',
  list_subscriptions: 'List the strategies you follow',
  list_shadow_decisions: 'Read test book decisions',
  list_shadow_pnl: 'Read trial results',
  get_shadow_pnl: 'Read trial results',
  // Risk and safety
  list_halts: 'Check whether trading is stopped',
  get_live_risk: 'Read live risk',
  get_risk_policy: 'Read the risk limits',
  get_my_risk_limits: 'Read your risk limits',
  list_risk_snapshots: 'Read risk history',
  engage_kill_switch: 'Stop trading',
  // Alerts and feed
  list_notifications: 'Read your notifications',
  mark_notifications_read: 'Mark notifications read',
  list_alerts: 'Read system alerts',
  list_price_alerts: 'List your price alerts',
  list_price_alert_events: 'Read when price alerts fired',
  create_price_alert: 'Create a price alert',
  update_price_alert: 'Change a price alert',
  delete_price_alert: 'Delete a price alert',
  // Studio
  get_rule_schema: 'Read the rule format',
  validate_rule_spec: 'Check a rule strategy',
  list_studio_templates: 'List Studio templates',
  get_studio_capabilities: 'Read what Studio can build',
  create_draft: 'Create a strategy draft',
  list_drafts: 'List strategy drafts',
  get_draft: 'Read a strategy draft',
  update_draft: 'Change a strategy draft',
  validate_draft: 'Check a strategy draft',
  run_draft_backtest: 'Backtest a draft',
  run_draft_lab: 'Test a draft in the lab',
  // Lab
  run_backtest: 'Start a backtest',
  run_lab: 'Start a lab run',
  run_sweep: 'Start a sweep',
  run_signal_ic: 'Start a signal study',
  get_job: 'Check a job',
  list_jobs: 'List your jobs',
  wait_for_job: 'Wait for a job',
  cancel_job: 'Cancel a job',
  list_survival_tests: 'List lab tests',
  list_survival_presets: 'List lab suites',
  list_cost_models: 'List cost models',
  list_ledger_runs: 'Read the trial ledger',
  get_ledger_run: 'Read a lab run',
  // Orders (drafts only, approved in the web app)
  draft_order: 'Draft an order for you to approve',
  list_order_drafts: 'List order drafts',
};

/** Tools whose confirm step is styled as a danger (they stop or delete). */
const DANGER = new Set(['engage_kill_switch', 'delete_price_alert']);

export function toolLabel(name: string): string {
  const known = TOOL_LABELS[name];
  if (known) return known;
  const words = name.replace(/[_-]+/g, ' ').trim();
  return words ? words.charAt(0).toUpperCase() + words.slice(1) : 'Use a tool';
}

export function isDangerTool(name: string): boolean {
  return DANGER.has(name);
}

/** Argument names in words: `portfolio_id` reads "Portfolio id". */
export function argLabel(name: string): string {
  const words = name.replace(/[_-]+/g, ' ').trim();
  return words ? words.charAt(0).toUpperCase() + words.slice(1) : name;
}

/** A short, readable value for an argument (clipped). */
export function argValue(value: unknown, max = 120): string {
  let text: string;
  if (value === null || value === undefined) text = 'none';
  else if (typeof value === 'string') text = value;
  else if (typeof value === 'boolean') text = value ? 'yes' : 'no';
  else if (typeof value === 'number') text = String(value);
  else text = JSON.stringify(value);
  return text.length > max ? `${text.slice(0, max - 1)}…` : text;
}

/** A tool result as indented text for the "What it saw" fold (clipped). */
export function resultText(value: unknown, max = 4000): string {
  if (value === undefined) return '';
  const text = typeof value === 'string' ? value : JSON.stringify(value, null, 2);
  return text.length > max ? `${text.slice(0, max)}\n…` : text;
}
