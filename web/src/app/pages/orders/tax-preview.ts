import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  input,
  resource,
} from '@angular/core';

import type { TaxPreviewView } from '../../api/models';
import { TaxService } from '../../api/tax.service';
import { formatDate, formatMoney, formatNumber, toneClass } from '../../core/format/format';

/** What the preview asks about. */
export interface TaxQuestion {
  ticker: string;
  side: 'buy' | 'sell';
  quantity: number;
  price?: number | null;
  /** The portfolio (default: the picked one). */
  portfolioId?: string | null;
}

const TICKER = /^[A-Z0-9][A-Z0-9._\-^=]{0,31}$/;

/** "Held 2 years, long term" style words for a lot's holding period. */
export function holdingLabel(period: 'short' | 'long'): string {
  return period === 'long' ? 'Long term' : 'Short term';
}

/** Whether the preview has anything to show: lots closed or a warning. */
export function worthShowing(p: TaxPreviewView): boolean {
  return p.lots.length > 0 || !!p.wash_sale_warning;
}

/**
 * The tax side of an order before it is placed: the lots a sell closes under
 * the portfolio's lot method, the gain and holding period, the estimated tax
 * at your configured rate, the after-tax proceeds and a US wash sale warning.
 * Shows nothing for a trade that closes no lot and warns of nothing, and
 * nothing when the estimate fails: it never blocks the ticket.
 */
@Component({
  selector: 'app-tax-preview',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    @if (preview(); as p) {
      <section class="tax" aria-label="Tax preview">
        <p class="head">
          <strong>Tax preview</strong>
          <span class="muted">An estimate, not tax advice.</span>
        </p>
        @if (p.wash_sale_warning; as w) {
          <p class="warning" role="status">{{ w }}</p>
        }
        @if (p.lots.length) {
          <table class="lots">
            <caption class="visually-hidden">
              Lots this order closes
            </caption>
            <thead>
              <tr>
                <th scope="col">Bought</th>
                <th scope="col" class="num">Shares</th>
                <th scope="col">Held</th>
                <th scope="col" class="num">Gain</th>
              </tr>
            </thead>
            <tbody>
              @for (lot of p.lots; track lot.open_fill_id) {
                <tr>
                  <td data-label="Bought">{{ day(lot.acquired) }}</td>
                  <td data-label="Shares" class="num">{{ num(lot.quantity) }}</td>
                  <td data-label="Held">{{ held(lot.holding_period) }}</td>
                  <td data-label="Gain" class="num" [class]="tone(lot.gain)">
                    {{ money(lot.gain, p.currency) }}
                  </td>
                </tr>
              }
            </tbody>
          </table>
          <dl class="sums">
            <div>
              <dt>Realised gain</dt>
              <dd class="num" [class]="tone(p.realized_gain)">
                {{ money(p.realized_gain, p.currency) }}
              </dd>
            </div>
            <div>
              <dt>Estimated tax</dt>
              <dd class="num">{{ money(p.estimated_tax, p.currency) }}</dd>
            </div>
            @if (p.side === 'sell') {
              <div>
                <dt>After tax</dt>
                <dd class="num">{{ money(p.after_tax_proceeds, p.currency) }}</dd>
              </div>
            }
            <div>
              <dt>This year's tax</dt>
              <dd class="num">
                {{ signedMoney(p.year_tax_change, p.currency) }}
              </dd>
            </div>
          </dl>
        }
      </section>
    }
  `,
  styles: `
    @use 'breakpoints' as bp;

    :host {
      display: block;
    }
    .tax {
      display: grid;
      gap: var(--space-2);
      padding: var(--space-3);
      border: 1px solid var(--color-border);
      border-radius: var(--radius-md);
      background: var(--color-surface);
      font-size: var(--text-sm);
    }
    .head {
      display: flex;
      flex-wrap: wrap;
      gap: var(--space-2);
      align-items: baseline;
    }
    .warning {
      padding: var(--space-2) var(--space-3);
      border-left: 3px solid var(--color-warn);
      border-radius: var(--radius-sm);
      background: var(--color-warn-soft);
      overflow-wrap: anywhere;
    }
    .lots {
      width: 100%;
      border-collapse: collapse;
    }
    .lots th,
    .lots td {
      padding: var(--space-1) var(--space-2);
      text-align: left;
      border-bottom: 1px solid var(--color-border);
    }
    .lots .num {
      text-align: right;
    }
    .sums {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(8rem, 1fr));
      gap: var(--space-2);
    }
    .sums dt {
      color: var(--color-ink-2);
    }
    .gain {
      color: var(--color-gain);
    }
    .loss {
      color: var(--color-loss);
    }
    @include bp.phone {
      .lots thead {
        position: absolute;
        width: 1px;
        height: 1px;
        overflow: hidden;
        clip: rect(0 0 0 0);
      }
      .lots tr {
        display: grid;
        grid-template-columns: 1fr 1fr;
        padding: var(--space-1) 0;
        border-bottom: 1px solid var(--color-border);
      }
      .lots td {
        border: 0;
      }
      .lots td::before {
        content: attr(data-label) ': ';
        color: var(--color-ink-2);
      }
      .lots .num {
        text-align: left;
      }
    }
  `,
})
export class TaxPreviewPanel {
  private readonly api = inject(TaxService);

  /** The order to estimate, or null for nothing to show. */
  readonly question = input<TaxQuestion | null>(null);

  // Equal by value: a caller may build a new question object on every
  // change detection, and that must not ask the server again.
  private readonly asked = computed<TaxQuestion | undefined>(
    () => {
      const q = this.question();
      if (!q) return undefined;
      const ticker = q.ticker.trim().toUpperCase();
      if (!TICKER.test(ticker) || !(q.quantity > 0)) return undefined;
      return { ...q, ticker };
    },
    { equal: (a, b) => JSON.stringify(a) === JSON.stringify(b) },
  );

  protected readonly estimate = resource({
    params: () => this.asked(),
    loader: ({ params }) =>
      this.api.preview(
        params.ticker,
        params.side,
        params.quantity,
        params.price ?? null,
        params.portfolioId ?? null,
      ),
  });

  protected readonly preview = computed<TaxPreviewView | null>(() => {
    if (!this.asked() || !this.estimate.hasValue()) return null;
    const p = this.estimate.value();
    return worthShowing(p) ? p : null;
  });

  protected readonly day = formatDate;
  protected readonly held = holdingLabel;
  protected readonly tone = toneClass;
  protected num(v: number): string {
    return formatNumber(v);
  }
  protected money(v: number, currency: string): string {
    return formatMoney(v, { currency });
  }
  protected signedMoney(v: number, currency: string): string {
    return formatMoney(v, { currency, signed: true });
  }
}
