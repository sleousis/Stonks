import type { SubscriptionView } from '../../api/subscriptions.service';
import { toTraderWords } from '../governance-labels';

/** Approve each trade and Automatic both pass the auto gate first. */
export function isGatedMode(mode: string): boolean {
  return mode === 'approve' || mode === 'auto';
}

/**
 * The unlock rule, said once above a list of follows (M8): the two gated
 * modes open after this many paper days (decision 2026-09-26: 20 trading
 * days).
 */
export function unlockRule(required: number): string {
  return `Approve each trade and Automatic unlock after ${required} paper days.`;
}

/** "Paper days: 4 of 20.", or null once the count is met (F4). */
export function paperProgress(sub: SubscriptionView): string | null {
  const needed = sub.paper_days_required;
  if (sub.paper_days_completed >= needed) return null;
  return `Paper days: ${sub.paper_days_completed} of ${needed}.`;
}

/** Whatever else the server's auto gate reported, besides the paper-day count. */
export function otherBlockers(sub: SubscriptionView): string[] {
  return sub.auto_blockers
    .filter((b) => !/paper trading day/i.test(b))
    .map((b) => sentence(toTraderWords(b).replace(/\bauto mode\b/gi, 'Automatic')));
}

/**
 * Why Approve each trade and Automatic are closed for this follow, in plain
 * words, or null when they may be turned on. The count of days on Paper
 * comes first, then anything else the server's auto gate reported.
 *
 * `compact` is for a list that says the unlock rule once above it
 * (`unlockRule()`): each row then says only how far it has come.
 */
export function autoBlockedReason(
  sub: SubscriptionView,
  opts: { compact?: boolean } = {},
): string | null {
  const needed = sub.paper_days_required;
  const days = opts.compact
    ? paperProgress(sub)
    : sub.paper_days_completed < needed
      ? `Approve each trade and Automatic open after ${needed} trading days on Paper. ` +
        `${sub.paper_days_completed} of ${needed} done.`
      : null;
  const reasons = [days, ...otherBlockers(sub)].filter((r): r is string => r !== null);
  return reasons.length ? reasons.join(' ') : null;
}

function sentence(text: string): string {
  const t = text.trim();
  const capital = t.charAt(0).toUpperCase() + t.slice(1);
  return /[.!?]$/.test(capital) ? capital : `${capital}.`;
}
