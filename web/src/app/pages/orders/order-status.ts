import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import type { OrderView } from '../../api/models';
import { StatusPill, type PillTone } from '../../shared/ui/status-pill';

export interface OrderStatusView {
  /** The raw status, for the pill's data attribute and filters. */
  status: string;
  label: string;
  tone: PillTone;
  /** Why the broker or the tick refused the order, when it said. */
  reason: string | null;
}

const ORDER_STATUSES: Record<string, { label: string; tone: PillTone }> = {
  pending: { label: 'Pending', tone: 'progress' },
  submitted: { label: 'Submitted', tone: 'progress' },
  filled: { label: 'Filled', tone: 'positive' },
  partially_filled: { label: 'Partially filled', tone: 'warn' },
  rejected: { label: 'Rejected', tone: 'negative' },
  cancelled: { label: 'Cancelled', tone: 'neutral' },
};

/** Status values the orders filter offers, in lifecycle order. */
export const ORDER_STATUS_OPTIONS = Object.entries(ORDER_STATUSES).map(([value, s]) => ({
  value,
  label: s.label,
}));

/**
 * Maps an order status (and the reason the ledger recorded, if any) to what
 * the pill shows. Unknown statuses keep their text and a neutral tone.
 */
export function orderStatusView(
  status: string | null | undefined,
  reason?: string | null,
): OrderStatusView {
  const key = (status ?? '').toLowerCase();
  const known = ORDER_STATUSES[key];
  const label = known?.label ?? (key ? key.replace(/_/g, ' ') : 'Unknown');
  const text = reason?.trim() || null;
  return {
    status: key || 'unknown',
    label,
    tone: known?.tone ?? 'neutral',
    // Only a refusal needs its reason on screen; a filled order's note is noise.
    reason: key === 'rejected' || key === 'cancelled' ? text : null,
  };
}

/** Why the ledger says the order ended in its status (`status_reason`). */
export function orderReason(order: OrderView): string | null {
  return order.status_reason ?? null;
}

/**
 * Order status pill plus the rejection reason underneath, so the reason is
 * visible on screen (not hidden in a tooltip) on every device.
 *
 *   <app-order-status [status]="o.status" [reason]="reason(o)" />
 */
@Component({
  selector: 'app-order-status',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [StatusPill],
  template: `
    <app-status-pill [status]="view().status" [label]="view().label" [tone]="view().tone" />
    @if (view().reason; as reason) {
      <span class="reason"><span class="visually-hidden">Reason: </span>{{ reason }}</span>
    }
  `,
  styles: `
    :host {
      display: inline-grid;
      justify-items: start;
      gap: 2px;
      min-width: 0;
    }
    .reason {
      max-width: 32ch;
      font-size: var(--text-xs);
      line-height: var(--leading-tight);
      color: var(--color-loss);
      overflow-wrap: anywhere;
      white-space: normal;
    }
  `,
})
export class OrderStatus {
  readonly status = input.required<string | null | undefined>();
  readonly reason = input<string | null>(null);
  protected readonly view = computed(() => orderStatusView(this.status(), this.reason()));
}
