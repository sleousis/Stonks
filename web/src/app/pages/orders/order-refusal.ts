import { ChangeDetectionStrategy, Component, input, model } from '@angular/core';

import type { RiskAdjustmentView } from '../../api/models';
import { formatNumber } from '../../core/format/format';
import { ApiError } from '../../core/http/api-error';

/** Why a manual order was not placed, as the ticket shows it. */
export interface OrderRefusal {
  /** The server's reason, ready to show. */
  message: string;
  /** What each risk rule would have done to the order. */
  adjustments: readonly RiskAdjustmentView[];
  /**
   * The smaller quantity the rules allow, when they shrank the order instead
   * of dropping it. Null when nothing smaller would pass.
   */
  allowed: number | null;
}

/** A 409 `order_refused` as an OrderRefusal, or null for any other failure. */
export function refusalOf(err: unknown): OrderRefusal | null {
  if (!(err instanceof ApiError) || err.status !== 409 || err.code !== 'order_refused') {
    return null;
  }
  const raw = err.problem['risk_adjustments'];
  const adjustments = Array.isArray(raw) ? (raw as RiskAdjustmentView[]) : [];
  return { message: err.message, adjustments, allowed: allowedQuantity(adjustments) };
}

/** The quantity left after every rule, when it is above zero and below the ask. */
export function allowedQuantity(adjustments: readonly RiskAdjustmentView[]): number | null {
  if (adjustments.length === 0) return null;
  const asked = Math.max(...adjustments.map((a) => a.original_quantity));
  const left = Math.min(...adjustments.map((a) => a.adjusted_quantity));
  return left > 0 && left < asked ? left : null;
}

/** A rule id (`max_position_weight`) in plain words ("Max position weight"). */
export function ruleLabel(rule: string): string {
  const words = rule.replace(/[_-]+/g, ' ').trim();
  return words ? words[0].toUpperCase() + words.slice(1) : 'A risk rule';
}

/** "Cuts 100 to 40" or "Drops the order", for one rule's adjustment. */
export function adjustmentText(a: RiskAdjustmentView): string {
  if (a.adjusted_quantity <= 0) return 'Drops the order';
  return `Cuts ${formatNumber(a.original_quantity)} to ${formatNumber(a.adjusted_quantity)}`;
}

/**
 * The refusal of a manual order: the server's reason, each rule's
 * adjustment, and, when the rules would allow a smaller order, the choice to
 * accept it (`allow_reduce`). The host checks the order again when the
 * choice changes.
 *
 *   <app-order-refusal [refusal]="r" [(allowReduce)]="allowReduce" />
 */
@Component({
  selector: 'app-order-refusal',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="refusal" role="alert">
      <p class="head">Not placed</p>
      <p class="message">{{ refusal().message }}</p>
      @if (refusal().adjustments.length) {
        <ul class="rules" aria-label="What each rule does">
          @for (a of refusal().adjustments; track $index) {
            <li>
              <span class="rule">{{ label(a.rule) }}</span>
              <span class="change num">{{ text(a) }}</span>
              @if (a.reason) {
                <span class="why">{{ a.reason }}</span>
              }
            </li>
          }
        </ul>
      }
      @if (refusal().allowed; as allowed) {
        <label class="check">
          <input
            type="checkbox"
            [checked]="allowReduce()"
            (change)="allowReduce.set($any($event.target).checked)"
          />
          Accept a smaller order of <span class="num">{{ num(allowed) }}</span>
        </label>
      }
    </div>
  `,
  styles: `
    :host {
      display: block;
      min-width: 0;
    }
    .refusal {
      display: grid;
      gap: var(--space-2);
      padding: var(--space-3) var(--space-4);
      border: 1px solid var(--color-border-strong);
      border-left: 3px solid var(--color-loss);
      border-radius: var(--radius-sm);
      background: var(--color-loss-soft);
    }
    .head {
      font-weight: var(--weight-semibold);
    }
    .message {
      overflow-wrap: anywhere;
    }
    .rules {
      display: grid;
      gap: var(--space-2);
      margin: 0;
      padding: 0;
      list-style: none;
    }
    .rules li {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 2px var(--space-3);
      padding-top: var(--space-2);
      border-top: 1px dashed var(--color-border-strong);
      font-size: var(--text-sm);
    }
    .rule {
      font-weight: var(--weight-semibold);
      overflow-wrap: anywhere;
    }
    .why {
      grid-column: 1 / -1;
      color: var(--color-ink-2);
      overflow-wrap: anywhere;
    }
  `,
})
export class OrderRefusalPanel {
  readonly refusal = input.required<OrderRefusal>();
  readonly allowReduce = model(false);
  protected readonly label = ruleLabel;
  protected readonly text = adjustmentText;
  protected readonly num = (v: number) => formatNumber(v);
}
