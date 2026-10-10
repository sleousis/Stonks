import { ChangeDetectionStrategy, Component, input, model } from '@angular/core';
import { RouterLink } from '@angular/router';

import type { RiskAdjustmentView } from '../../api/models';
import { formatMoney, formatNumber } from '../../core/format/format';
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
  /** Trading is stopped by a halt: the panel links to the halts page. */
  halted?: boolean;
}

/** What the plain words need to know about the order's portfolio. */
export interface RefusalContext {
  /** The portfolio's display name ("Jamie's paper portfolio"). */
  portfolio: string;
  currency: string;
}

const DEFAULT_CONTEXT: RefusalContext = { portfolio: 'this portfolio', currency: 'USD' };

/** A 409 `order_refused` as an OrderRefusal, or null for any other failure. */
export function refusalOf(err: unknown, ctx: RefusalContext = DEFAULT_CONTEXT): OrderRefusal | null {
  if (!(err instanceof ApiError) || err.status !== 409 || err.code !== 'order_refused') {
    return null;
  }
  const raw = err.problem['risk_adjustments'];
  const adjustments = Array.isArray(raw) ? (raw as RiskAdjustmentView[]) : [];
  const { message, halted } = plainRefusal(err.message, adjustments, ctx);
  return { message, adjustments, allowed: allowedQuantity(adjustments), halted };
}

/** "10.0" as "10" and "1,234.50" as is: numbers as a trader writes them. */
function plainNumber(raw: string): string {
  const n = Number(raw.replace(/,/g, ''));
  return Number.isFinite(n) ? formatNumber(n) : raw;
}

/** First letter capital, a full stop at the end, no internal ids. */
export function tidyMessage(raw: string): string {
  let text = raw
    .replace(/\s*\((?:portfolio|user)\s+[^)]*\)/gi, '')
    .replace(/\b(\d+)\.0\b/g, '$1')
    .trim();
  if (!text) return 'The order was not placed.';
  text = text[0].toUpperCase() + text.slice(1);
  return /[.!?]$/.test(text) ? text : `${text}.`;
}

/**
 * The server's refusal in plain words for a trader: what went wrong and what
 * to do next, with no rule ids, portfolio ids or float quantities.
 */
export function plainRefusal(
  message: string,
  adjustments: readonly RiskAdjustmentView[],
  ctx: RefusalContext = DEFAULT_CONTEXT,
): { message: string; halted: boolean } {
  const m = message.trim();
  const plain = (text: string) => ({ message: text, halted: false });
  if (/^trading is halted/i.test(m)) {
    return {
      message: `Trading is stopped for ${ctx.portfolio}. Resume it to place orders.`,
      halted: true,
    };
  }
  const cash = adjustments.some((a) => a.rule === 'cash_buffer' && a.adjusted_quantity <= 0);
  if (/^the risk rules refuse this order/i.test(m)) {
    if (cash || /cash_buffer/.test(m)) {
      const free = formatMoney(0, { currency: ctx.currency });
      return plain(
        `Not enough cash. You have ${free} free, so this buy cannot go through. ` +
          'Sell something first or lower the quantity.',
      );
    }
    return plain('The risk limits refuse this order. Each limit is listed below.');
  }
  const allow = /^the risk rules allow ([\d.,]+) of the ([\d.,]+) asked for/i.exec(m);
  if (allow) {
    return plain(
      `The risk limits allow ${plainNumber(allow[1])} of the ${plainNumber(allow[2])} you asked for.`,
    );
  }
  const side = /^the (stop|target) of a (buy|short sale) must be (below|above)/i.exec(m);
  if (side) {
    const what = side[1].toLowerCase();
    const buy = side[2].toLowerCase() === 'buy';
    return plain(`For a ${buy ? 'buy' : 'sale'}, the ${what} goes ${side[3].toLowerCase()} the price.`);
  }
  if (/would close a position and open the other way/i.test(m)) {
    return plain(
      'This order would close your position and open one the other way. ' +
        'Place the close and the new position as two orders.',
    );
  }
  return plain(tidyMessage(m));
}

/** The quantity left after every rule, when it is above zero and below the ask. */
export function allowedQuantity(adjustments: readonly RiskAdjustmentView[]): number | null {
  if (adjustments.length === 0) return null;
  const asked = Math.max(...adjustments.map((a) => a.original_quantity));
  const left = Math.min(...adjustments.map((a) => a.adjusted_quantity));
  return left > 0 && left < asked ? left : null;
}

const RULE_LABELS: Record<string, string> = {
  cash_buffer: 'Cash on hand',
  max_position_weight: 'Largest position',
};

/** A rule id (`max_position_weight`) in plain words ("Largest position"). */
export function ruleLabel(rule: string): string {
  if (RULE_LABELS[rule]) return RULE_LABELS[rule];
  const words = rule.replace(/[_-]+/g, ' ').trim();
  return words ? words[0].toUpperCase() + words.slice(1) : 'A risk rule';
}

/** "Cuts 100 to 40" or "Drops the order", for one rule's adjustment. */
export function adjustmentText(a: RiskAdjustmentView): string {
  if (a.adjusted_quantity <= 0) return 'Drops the order';
  return `Cuts ${formatNumber(a.original_quantity)} to ${formatNumber(a.adjusted_quantity)}`;
}

/**
 * A rule's own reason when it reads well, or null for the engine's
 * "quantity 10.0 exceeds cash_buffer room 0.0", which the rule's change says
 * better.
 */
export function ruleReason(a: RiskAdjustmentView): string | null {
  const reason = (a.reason ?? '').trim();
  if (!reason || /\bexceeds\s+\S+\s+room\b/.test(reason)) return null;
  return tidyMessage(reason);
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
  imports: [RouterLink],
  template: `
    <div class="refusal" role="alert">
      <p class="head">Not placed</p>
      <p class="message">{{ refusal().message }}</p>
      @if (refusal().halted) {
        <a class="btn" routerLink="/ops/halts">Resume trading</a>
      }
      @if (refusal().adjustments.length) {
        <ul class="rules" aria-label="What each rule does">
          @for (a of refusal().adjustments; track $index) {
            <li>
              <span class="rule">{{ label(a.rule) }}</span>
              <span class="change num">{{ text(a) }}</span>
              @if (why(a); as reason) {
                <span class="why">{{ reason }}</span>
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
    .btn {
      justify-self: start;
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
  protected readonly why = ruleReason;
  protected readonly num = (v: number) => formatNumber(v);
}
