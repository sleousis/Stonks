import type { LeaderboardRow, StrategyMetadataView } from '../app/api/models';

/** Metadata every StrategySummary / StrategyDetail fixture needs. */
export const STRATEGY_METADATA: StrategyMetadataView = {
  alpha_family: 'trend',
  hypothesis: 'Recent winners keep winning for a while.',
  label_horizon_bars: 21,
  premise: 'trend',
  required_history_bars: 126,
};

/** A strategy's paper figures for leaderboard and tear sheet fixtures. */
export function paper(overrides: Partial<LeaderboardRow['paper']> = {}): LeaderboardRow['paper'] {
  return {
    days: 40,
    first_day: '2026-08-01',
    latest_day: '2026-09-25',
    latest_value: 10_300,
    total_return: 0.03,
    cagr: 0.2,
    sharpe: 1.4,
    sortino: 2.1,
    max_drawdown: -0.04,
    volatility: 0.12,
    trades: 6,
    ...overrides,
  };
}
