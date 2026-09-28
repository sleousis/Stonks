import type { HaltView } from '../../api/models';
import { killStopsText } from '../../core/halts/kill-ticket';

export const HALT_KIND_LABEL: Record<HaltView['kind'], string> = {
  kill: 'Stop trading',
  month_loss: 'Monthly loss',
  week_loss: 'Weekly loss',
  drawdown: 'Drawdown',
  operational: 'Operational',
  runaway: 'Runaway orders',
  broker_drift: 'Broker drift',
  intraday_loss: 'Intraday loss',
};

/** "Stops: New buys", never "Buys only", which reads as "only buys allowed" (UX-66). */
export const HALT_STOPS_LABEL: Record<HaltView['halt'], string> = {
  all: killStopsText(false),
  buys: killStopsText(true),
};

/** What ends a halt of this kind, for the action column. */
export function haltAction(h: HaltView): 'resume' | 'clear' {
  return h.kind === 'kill' ? 'resume' : 'clear';
}
