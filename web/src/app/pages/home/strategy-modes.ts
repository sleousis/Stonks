import type { SubscriptionView } from '../../api/subscriptions.service';

import { toTraderWords } from '../../shared/governance-labels';

export { MODES, type ModeOption } from '../../shared/governance-labels';

/**
 * The unlock rule, said once above the list (M8): the two real-money modes
 * open after this many paper days (decision 2026-09-26: 20 trading days).
 */
export function unlockRule(required: number): string {
  return `Approve each trade and Automatic unlock after ${required} paper days.`;
}

/** "Paper days: 4 of 20", or null once the count is met (F4). */
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
 * Why the real-money modes (approve each trade, automatic) are off limits
 * for this follow, in plain words, or null when they may be turned on. The
 * paper-day count comes first, then anything else the auto gate reported.
 */
export function autoBlockedReason(sub: SubscriptionView): string | null {
  const reasons = [paperProgress(sub), ...otherBlockers(sub)].filter(
    (r): r is string => r !== null,
  );
  return reasons.length ? reasons.join(' ') : null;
}

function sentence(text: string): string {
  const t = text.trim();
  const capital = t.charAt(0).toUpperCase() + t.slice(1);
  return /[.!?]$/.test(capital) ? capital : `${capital}.`;
}
