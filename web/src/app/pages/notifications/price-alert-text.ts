import type { PriceAlertView, WatchlistView } from '../../api/models';
import { formatNumber } from '../../core/format/format';

export type AlertCondition = PriceAlertView['condition'];

/** The condition choices, in the words the editor shows. */
export const CONDITIONS: readonly { value: AlertCondition; label: string }[] = [
  { value: 'crosses_above', label: 'Rises above' },
  { value: 'crosses_below', label: 'Falls below' },
  { value: 'moves_pct', label: 'Moves by' },
];

/** "Rises above 250", "Moves 8% either way in 5 days". */
export function conditionText(
  a: Pick<PriceAlertView, 'condition' | 'level' | 'pct' | 'window_days'>,
): string {
  switch (a.condition) {
    case 'crosses_above':
      return `Rises above ${formatNumber(a.level)}`;
    case 'crosses_below':
      return `Falls below ${formatNumber(a.level)}`;
    case 'moves_pct': {
      const days = a.window_days ?? 1;
      return `Moves ${formatNumber(a.pct)}% either way in ${days} ${days === 1 ? 'day' : 'days'}`;
    }
    default:
      return 'Unknown condition';
  }
}

/** The ticker, or the watchlist's name ("Tech watchlist"). */
export function targetText(
  a: Pick<PriceAlertView, 'target_kind' | 'ticker' | 'watchlist_id'>,
  watchlists: readonly WatchlistView[],
): string {
  if (a.target_kind === 'ticker') return a.ticker ?? 'A ticker';
  const list = watchlists.find((w) => w.id === a.watchlist_id);
  return list ? `Every ticker in ${list.name}` : 'Every ticker in a watchlist';
}

/** The alert's own name, else its target. */
export function alertTitle(a: PriceAlertView, watchlists: readonly WatchlistView[]): string {
  return a.name?.trim() || targetText(a, watchlists);
}
