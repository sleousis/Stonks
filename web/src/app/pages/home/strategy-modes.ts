import type { SubscriptionMode, SubscriptionView } from '../../api/subscriptions.service';

export interface ModeOption {
  value: SubscriptionMode;
  label: string;
  /** One line under the switch when this mode is on. */
  help: string;
}

export const MODES: readonly ModeOption[] = [
  { value: 'notify', label: 'Notify', help: 'You get the signals. Nothing is traded.' },
  { value: 'paper', label: 'Paper', help: 'Trades on paper with pretend money.' },
  { value: 'auto', label: 'Auto', help: 'Places real orders with your broker.' },
];

/**
 * Why auto is off limits for this subscription, in plain words, or null
 * when it may be turned on. The paper-day count comes first (decision
 * 2026-09-26: 20 trading days in paper), then anything else the server's
 * auto gate reported.
 */
export function autoBlockedReason(sub: SubscriptionView): string | null {
  const reasons: string[] = [];
  const needed = sub.paper_days_required;
  if (sub.paper_days_completed < needed) {
    reasons.push(
      `Auto unlocks after ${needed} paper trading days. ${sub.paper_days_completed} of ${needed} done.`,
    );
  }
  for (const blocker of sub.auto_blockers) {
    if (/paper trading day/i.test(blocker)) continue;
    reasons.push(sentence(blocker));
  }
  return reasons.length ? reasons.join(' ') : null;
}

function sentence(text: string): string {
  const t = text.trim();
  const capital = t.charAt(0).toUpperCase() + t.slice(1);
  return /[.!?]$/.test(capital) ? capital : `${capital}.`;
}
