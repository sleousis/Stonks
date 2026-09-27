import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import type { OrderView } from '../../api/models';
import { StatusPill, type PillTone } from '../../shared/ui/status-pill';

export interface OrderStatusView {
  /** The raw state (or status), for the pill's data attribute and filters. */
  status: string;
  label: string;
  tone: PillTone;
  /** Why the broker or the trading run refused the order, when it said. */
  reason: string | null;
  /** What an unsettled state means for the trader, in plain words. */
  note: string | null;
}

interface StateWords {
  label: string;
  tone: PillTone;
  /** Shown under the pill for states that need a word of explanation. */
  note?: string;
}

/**
 * Every fine order state (`orders.state`) in plain words. The coarse
 * `status` shares the words of its values.
 */
export const ORDER_STATES: Record<string, StateWords> = {
  pending: { label: 'Pending', tone: 'progress' },
  submitted: {
    label: 'Sent',
    tone: 'progress',
    note: 'Sent to the broker, not confirmed yet.',
  },
  accepted: {
    label: 'Working',
    tone: 'progress',
    note: 'The broker has the order and it is waiting to fill.',
  },
  partially_filled: { label: 'Partially filled', tone: 'warn' },
  filled: { label: 'Filled', tone: 'positive' },
  pending_cancel: {
    label: 'Cancelling',
    tone: 'warn',
    note: 'Cancel sent. It can still fill until the broker confirms.',
  },
  cancelled: { label: 'Cancelled', tone: 'neutral' },
  expired: {
    label: 'Expired',
    tone: 'neutral',
    note: 'The order ran out of time without filling.',
  },
  rejected: { label: 'Rejected', tone: 'negative' },
  unknown: {
    label: 'Outcome unknown',
    tone: 'warn',
    note: 'The broker did not answer in time. Nothing is sent again until Stonks finds the order at the broker.',
  },
};

/** The coarse statuses the orders filter offers, in lifecycle order. */
const FILTER_STATUSES = [
  'pending',
  'submitted',
  'filled',
  'partially_filled',
  'rejected',
  'cancelled',
] as const;

/** Status values the orders filter offers, in lifecycle order. */
export const ORDER_STATUS_OPTIONS = FILTER_STATUSES.map((value) => ({
  value,
  label: ORDER_STATES[value].label,
}));

/**
 * Maps an order's state (the fine `state` when the ledger has one, else its
 * `status`) and the recorded reason to what the pill shows. Values the
 * console does not know keep their text and a neutral tone.
 */
export function orderStatusView(
  status: string | null | undefined,
  reason?: string | null,
  state?: string | null,
): OrderStatusView {
  const key = (state || status || '').toLowerCase();
  const known = ORDER_STATES[key];
  const label = known?.label ?? (key ? key.replace(/_/g, ' ') : 'No status');
  const text = reason?.trim() || null;
  return {
    status: key || 'none',
    label,
    tone: known?.tone ?? 'neutral',
    // Only a refusal needs its reason on screen; a filled order's note is noise.
    reason: key === 'rejected' || key === 'cancelled' || key === 'expired' ? text : null,
    note: known?.note ?? null,
  };
}

/** A stop order in plain words: what it is and when it trades. */
export interface StopWords {
  /** "Protective stop" or "Stop order". */
  label: string;
  /** When it trades, e.g. "Sells if the price falls to $90.50." */
  trigger: string;
  /** How long it lasts, for a protective stop. */
  lasts: string | null;
}

/** Plain words for a stop order, or null for any other order. */
export function stopWords(
  order: Pick<OrderView, 'side' | 'stop_price' | 'protective' | 'order_type'>,
  money: (value: number) => string,
): StopWords | null {
  const isStop = order.order_type === 'stop' || order.order_type === 'stop_limit';
  if (!isStop && !order.protective) return null;
  const at = order.stop_price != null ? money(order.stop_price) : null;
  const trigger =
    at === null
      ? 'Trades when the price reaches its stop.'
      : order.side === 'sell'
        ? `Sells if the price falls to ${at}.`
        : `Buys back if the price rises to ${at}.`;
  return order.protective
    ? {
        label: 'Protective stop',
        trigger,
        lasts: 'Works at the broker until the position closes, and follows its size.',
      }
    : { label: 'Stop order', trigger, lasts: null };
}

/** Why the ledger says the order ended in its status (`status_reason`). */
export function orderReason(order: OrderView): string | null {
  return order.status_reason ?? null;
}

/**
 * Order state pill plus, underneath, the rejection reason or what the state
 * means, so it is on screen (not hidden in a tooltip) on every device. The
 * fine `state` wins over `status` when the ledger has one.
 *
 *   <app-order-status [status]="o.status" [state]="o.state" [reason]="reason(o)" />
 */
@Component({
  selector: 'app-order-status',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [StatusPill],
  template: `
    <app-status-pill [status]="view().status" [label]="view().label" [tone]="view().tone" />
    @if (view().reason; as reason) {
      <span class="reason"><span class="visually-hidden">Reason: </span>{{ reason }}</span>
    } @else if (showNote() && view().note) {
      <span class="note">{{ view().note }}</span>
    }
  `,
  styles: `
    :host {
      display: inline-grid;
      justify-items: start;
      gap: 2px;
      min-width: 0;
    }
    .reason,
    .note {
      max-width: 32ch;
      font-size: var(--text-xs);
      line-height: var(--leading-tight);
      overflow-wrap: anywhere;
      white-space: normal;
    }
    .reason {
      color: var(--color-loss);
    }
    .note {
      color: var(--color-ink-2);
    }
  `,
})
export class OrderStatus {
  readonly status = input.required<string | null | undefined>();
  /** The fine order state; null on older orders, which only have a status. */
  readonly state = input<string | null | undefined>(null);
  readonly reason = input<string | null>(null);
  /** Off in compact lists, where only the pill fits. */
  readonly showNote = input(true);
  protected readonly view = computed(() =>
    orderStatusView(this.status(), this.reason(), this.state()),
  );
}
