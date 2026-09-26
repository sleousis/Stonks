import type { StrategyMetadataView } from '../app/api/models';

/** Metadata every StrategySummary / StrategyDetail fixture needs. */
export const STRATEGY_METADATA: StrategyMetadataView = {
  alpha_family: 'trend',
  hypothesis: 'Recent winners keep winning for a while.',
  label_horizon_bars: 21,
  premise: 'trend',
  required_history_bars: 126,
};
