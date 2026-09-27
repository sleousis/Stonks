import type { HaltView } from '../../api/models';

export const HALT_KIND_LABEL: Record<HaltView['kind'], string> = {
  kill: 'Kill switch',
  month_loss: 'Monthly loss',
  week_loss: 'Weekly loss',
  drawdown: 'Drawdown',
  operational: 'Operational',
  runaway: 'Runaway orders',
  broker_drift: 'Broker drift',
};

export const HALT_STOPS_LABEL: Record<HaltView['halt'], string> = {
  all: 'All orders',
  buys: 'Buys only',
};

/** What ends a halt of this kind, for the action column. */
export function haltAction(h: HaltView): 'resume' | 'clear' {
  return h.kind === 'kill' ? 'resume' : 'clear';
}
