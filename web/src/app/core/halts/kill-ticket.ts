import type { ConfirmTicket } from '../confirm/confirm.service';

/** What a kill switch stops, for tickets and the halts table (UX-66: "New buys", not "Buys only"). */
export function killStopsText(buysOnly: boolean): string {
  return buysOnly ? 'New buys' : 'All new orders';
}

export interface KillTicketInput {
  /** Who it covers, in trader words ("Portfolio Main book", "Your portfolios"). */
  scopeText: string;
  buysOnly: boolean;
  reason: string;
  /** Any covered portfolio trades real money. */
  live: boolean;
}

/**
 * The kill switch as an order ticket (UX-01, UX-51): who it covers, what
 * stops, what still goes out and why, with the PAPER or LIVE stamp. The
 * Stop trading sheet and the halts page both confirm with it.
 */
export function killTicket(k: KillTicketInput): ConfirmTicket {
  return {
    kind: 'Kill switch',
    live: k.live,
    lines: [
      { label: 'Scope', value: k.scopeText },
      { label: 'Stops', value: killStopsText(k.buysOnly) },
      { label: 'Still goes out', value: k.buysOnly ? 'Sells and exits' : 'Nothing new' },
      { label: 'Reason', value: k.reason.trim() || 'None given' },
    ],
  };
}
