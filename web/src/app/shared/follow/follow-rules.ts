import type { SubscriptionView } from '../../api/subscriptions.service';
import { toTraderWords } from '../governance-labels';

/** Approve each trade and Automatic both pass the auto gate first. */
export function isGatedMode(mode: string): boolean {
  return mode === 'approve' || mode === 'auto';
}

const PAPER_DAYS = /paper trading day/i;
/** The server says "auto mode needs a portfolio" or "... a broker portfolio" (PO-02). */
const NEEDS_PORTFOLIO = /needs a (broker )?portfolio/i;

/** Both gated modes need a broker portfolio, and this follow has none yet. */
export function needsBrokerPortfolio(sub: SubscriptionView): boolean {
  return sub.auto_blockers.some((b) => NEEDS_PORTFOLIO.test(b));
}

function shortOfPaperDays(sub: SubscriptionView): boolean {
  return sub.paper_days_completed < sub.paper_days_required;
}

/** "Paper days: 4 of 20.", or null once the count is met (F4). */
export function paperProgress(sub: SubscriptionView): string | null {
  if (!shortOfPaperDays(sub)) return null;
  return `Paper days: ${sub.paper_days_completed} of ${sub.paper_days_required}.`;
}

/** Whatever else the server's auto gate reported, besides paper days and the portfolio. */
export function otherBlockers(sub: SubscriptionView): string[] {
  return sub.auto_blockers
    .filter((b) => !PAPER_DAYS.test(b) && !NEEDS_PORTFOLIO.test(b))
    .map((b) => sentence(toTraderWords(b)));
}

/** Whether the gate keeps Approve each trade and Automatic closed for this follow. */
export function gatedModesLocked(sub: SubscriptionView): boolean {
  return shortOfPaperDays(sub) || sub.auto_blockers.length > 0;
}

/**
 * The one short line that stands in for the two closed modes (FR-03), or
 * null when they may be turned on. Both modes need a broker portfolio and
 * the paper days, so the line names both whenever both are missing.
 */
export function lockedModesLine(sub: SubscriptionView): string | null {
  if (!gatedModesLocked(sub)) return null;
  const broker = needsBrokerPortfolio(sub);
  const days = shortOfPaperDays(sub);
  const n = sub.paper_days_required;
  if (broker && days) return `More modes unlock with a broker portfolio after ${n} paper days.`;
  if (days) return `More modes unlock after ${n} paper days.`;
  if (broker) return 'More modes unlock with a broker portfolio.';
  return 'More modes are closed for now.';
}

/**
 * How far this follow has come and anything else the gate reported, in
 * plain words, or null when there is nothing to add. Alerts only follows
 * collect no paper days, so they get no count.
 */
export function blockerDetail(sub: SubscriptionView): string | null {
  const days = sub.mode === 'notify' ? null : paperProgress(sub);
  const reasons = [days, ...otherBlockers(sub)].filter((r): r is string => r !== null);
  return reasons.length ? reasons.join(' ') : null;
}

function sentence(text: string): string {
  const t = text.trim();
  const capital = t.charAt(0).toUpperCase() + t.slice(1);
  return /[.!?]$/.test(capital) ? capital : `${capital}.`;
}
