import type { HaltView } from '../../api/models';
import { haltScopeText } from './halt-state.service';

const KIND_TEXT: Record<HaltView['kind'], string> = {
  kill: 'kill switch',
  month_loss: 'monthly loss breaker',
  week_loss: 'weekly loss breaker',
  drawdown: 'drawdown breaker',
  operational: 'operational halt',
};

export interface HaltSummary {
  /** `kill` for a kill switch, `halt` for a breaker or operational halt. */
  tone: 'kill' | 'halt';
  title: string;
  text: string;
}

/** What the app says about the active halts, or null when trading is not halted. */
export function haltSummary(active: readonly HaltView[]): HaltSummary | null {
  const halts = active.filter((h) => h.active);
  if (!halts.length) return null;
  const kills = halts.filter((h) => h.kind === 'kill');
  const shown = kills.length ? kills : halts;
  const scopes = [...new Set(shown.map(haltScopeText))].join(', ');
  const what = halts.some((h) => h.halt === 'all')
    ? 'No new orders go out'
    : 'New buys are stopped, sells still go out';
  if (kills.length) {
    return {
      tone: 'kill',
      title: 'Kill switch on.',
      text: `${scopes}. ${what} until someone resumes trading.`,
    };
  }
  const kinds = [...new Set(halts.map((h) => KIND_TEXT[h.kind]))].join(', ');
  const title = halts.length === 1 ? 'Trading halted.' : `${halts.length} halts active.`;
  return { tone: 'halt', title, text: `${scopes}: ${kinds}. ${what} until it is cleared.` };
}
