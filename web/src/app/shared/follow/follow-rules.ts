import type { SubscriptionView } from '../../api/subscriptions.service';
import { toTraderWords } from '../governance-labels';

/** Approve each trade and Automatic both pass the auto gate first. */
export function isGatedMode(mode: string): boolean {
  return mode === 'approve' || mode === 'auto';
}

/**
 * Why Approve each trade and Automatic are closed for this follow, in plain
 * words, or null when they may be turned on. The count of days on Paper
 * comes first (decision 2026-09-26: 20 trading days), then anything else the
 * server's auto gate reported.
 */
export function autoBlockedReason(sub: SubscriptionView): string | null {
  const reasons: string[] = [];
  const needed = sub.paper_days_required;
  if (sub.paper_days_completed < needed) {
    reasons.push(
      `Approve each trade and Automatic open after ${needed} trading days on Paper. ` +
        `${sub.paper_days_completed} of ${needed} done.`,
    );
  }
  for (const blocker of sub.auto_blockers) {
    if (/paper trading day/i.test(blocker)) continue;
    reasons.push(sentence(toTraderWords(blocker)));
  }
  return reasons.length ? reasons.join(' ') : null;
}

function sentence(text: string): string {
  const t = text.trim();
  const capital = t.charAt(0).toUpperCase() + t.slice(1);
  return /[.!?]$/.test(capital) ? capital : `${capital}.`;
}
