import type { TicketStatus, TicketView } from '../../api/tickets.service';
import type { PillForm, PillTone } from '../../shared/ui/status-pill';

/** A ticket's status in trader words, with the pill it shows as. */
export interface TicketStatusLook {
  label: string;
  tone: PillTone;
  form: PillForm;
}

const LOOKS: Record<TicketStatus, TicketStatusLook> = {
  awaiting_approval: { label: 'Waiting for you', tone: 'warn', form: 'lamp' },
  approved: { label: 'Approved', tone: 'info', form: 'lamp' },
  rejected: { label: 'Rejected', tone: 'neutral', form: 'receipt' },
  expired: { label: 'Expired', tone: 'neutral', form: 'receipt' },
  submitted: { label: 'Sent', tone: 'progress', form: 'working' },
  filled: { label: 'Filled', tone: 'positive', form: 'receipt' },
  unfilled: { label: 'Not filled', tone: 'neutral', form: 'receipt' },
  cancelled: { label: 'Cancelled', tone: 'neutral', form: 'receipt' },
  failed: { label: 'Refused by the broker', tone: 'negative', form: 'receipt' },
};

export function ticketStatusLook(status: string): TicketStatusLook {
  return LOOKS[status as TicketStatus] ?? { label: status, tone: 'neutral', form: 'lamp' };
}

/** Why a ticket waits for a person, in one plain sentence. */
export function holdWords(hold: TicketView['hold']): string | null {
  if (hold === 'approve_mode') return 'You approve each trade of this strategy.';
  if (hold === 'runaway') {
    return 'Held for you: this run tried to close more positions than the limit allows.';
  }
  if (hold === 'hard_to_borrow') {
    return 'Held for you: this short sale is hard to borrow, so its borrow fee is high.';
  }
  if (hold === 'options') {
    return 'Held for you: every option order waits for your approval. Its limit sits at the mid.';
  }
  return null;
}

/** Who decided, in trader words: the system for auto books, else the person. */
export function deciderWords(actor: string | null | undefined): string {
  if (!actor) return '–';
  if (actor.startsWith('service:')) return 'Automatic';
  return 'You';
}

/** The rules that changed or checked the order, as short lines. */
export function ruleLines(ticket: TicketView): string[] {
  return ticket.rules
    .map((r) => {
      const rule = typeof r['rule'] === 'string' ? r['rule'].replace(/_/g, ' ') : 'rule';
      const reason = typeof r['reason'] === 'string' ? r['reason'] : '';
      return reason ? `${rule}: ${reason}` : rule;
    })
    .slice(0, 4);
}
