import type { HaltView } from '../../api/models';

const KIND_TEXT: Record<HaltView['kind'], string> = {
  kill: 'Stop trading',
  month_loss: 'monthly loss breaker',
  week_loss: 'weekly loss breaker',
  drawdown: 'drawdown breaker',
  operational: 'operational halt',
  runaway: 'runaway order halt',
  broker_drift: 'broker drift halt',
  intraday_loss: 'intraday loss halt',
};

export type HaltScope = Pick<HaltView, 'scope' | 'portfolio_id' | 'user_id'>;

/**
 * Who a halt covers, in trader words (UX-17): "Every portfolio", "Your
 * portfolios", "Portfolio Main book". Never an id: a portfolio whose name
 * is unknown is "One portfolio", another trader's is "A trader's portfolios".
 */
export function haltScopeText(
  h: HaltScope,
  names: ReadonlyMap<string, string> = new Map(),
  meId: string | null = null,
): string {
  if (h.scope === 'portfolio') {
    const name = h.portfolio_id ? names.get(h.portfolio_id) : undefined;
    return name ? `Portfolio ${name}` : 'One portfolio';
  }
  if (h.scope === 'user') {
    return !h.user_id || h.user_id === meId ? 'Your portfolios' : "A trader's portfolios";
  }
  return 'Every portfolio';
}

export interface HaltSummary {
  /** `kill` for a kill switch, `halt` for a breaker or operational halt. */
  tone: 'kill' | 'halt';
  title: string;
  text: string;
}

/**
 * What the app says about the active halts, or null when trading is not
 * halted. `scopeText` names who each halt covers (the shell passes one
 * that knows portfolio names).
 */
export function haltSummary(
  active: readonly HaltView[],
  scopeText: (h: HaltScope) => string = (h) => haltScopeText(h),
): HaltSummary | null {
  const halts = active.filter((h) => h.active);
  if (!halts.length) return null;
  const kills = halts.filter((h) => h.kind === 'kill');
  const shown = kills.length ? kills : halts;
  const scopes = [...new Set(shown.map(scopeText))].join(', ');
  const what = halts.some((h) => h.halt === 'all')
    ? 'No new orders go out'
    : 'New buys are stopped, sells still go out';
  if (kills.length) {
    return {
      tone: 'kill',
      title: 'Trading stopped.',
      text: `${scopes}. ${what} until someone resumes trading.`,
    };
  }
  const kinds = [...new Set(halts.map((h) => KIND_TEXT[h.kind]))].join(', ');
  const title = halts.length === 1 ? 'Trading halted.' : `${halts.length} halts active.`;
  return { tone: 'halt', title, text: `${scopes}: ${kinds}. ${what} until it is cleared.` };
}
