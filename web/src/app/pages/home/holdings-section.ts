import { ChangeDetectionStrategy, Component, computed, inject } from '@angular/core';
import { RouterLink } from '@angular/router';

import { formatMoney, formatNumber, formatPercent, toneClass } from '../../core/format/format';
import { EmptyState } from '../../shared/ui/states';
import { HomeStore } from './home-store';

const TOP_HOLDINGS = 8;

/**
 * Holdings on Today: the biggest positions first, each with its quantity,
 * last price, value, return and a weight bar (a 4px accent bar, no track).
 * On a phone each row becomes a list item: the ticker and quantity left,
 * the value and return right.
 */
@Component({
  selector: 'app-holdings-section',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink, EmptyState],
  template: `
    @if (book()) {
      <section class="page-section" aria-labelledby="home-holdings">
        <header class="section-head">
          <h2 id="home-holdings">
            Holdings
            @if (count()) {
              <span class="count">{{ count() }}</span>
            }
          </h2>
          <a routerLink="/orders" class="text-link">See orders</a>
        </header>
        @if (holdings().length === 0) {
          <div class="section-body">
            <app-empty-state
              compact
              title="No holdings yet"
              message="Holdings show here after your first paper or auto trade."
            />
          </div>
        } @else {
          <div class="grid head" aria-hidden="true">
            <span>Ticker</span>
            <span class="r">Qty</span>
            <span class="r wide">Last</span>
            <span class="r">Value</span>
            <span class="r">Return</span>
            <span class="r wide">Weight</span>
          </div>
          <ul class="holdings" aria-label="Holdings">
            @for (h of holdings(); track h.ticker) {
              <li class="grid">
                <span class="ticker">{{ h.ticker }}</span>
                <span class="qty num r">{{ h.quantity }}</span>
                <span class="num r wide">{{ h.price }}</span>
                <span class="value num r">{{ h.value }}</span>
                <span class="pnl num r" [class]="h.tone">{{ h.pnl }}</span>
                <span class="weight r wide">
                  @if (h.weight !== null) {
                    <span class="bar" [style.width.rem]="h.bar * 0.045" aria-hidden="true"></span>
                    <span class="num">{{ h.weightText }}</span>
                  }
                </span>
              </li>
            }
          </ul>
          @if (more() > 0) {
            <a routerLink="/insights" class="text-link more">And {{ more() }} more on Insights</a>
          }
        }
      </section>
    }
  `,
  styles: `
    @use 'breakpoints' as bp;
    :host {
      display: block;
      min-width: 0;
    }
    .grid {
      display: grid;
      grid-template-columns: minmax(0, 1fr) 4.5rem 6.5rem 7.5rem 5rem 8.5rem;
      gap: var(--space-4);
      align-items: center;
    }
    .head {
      min-height: 36px;
      border-bottom: 1px solid var(--rule);
      color: var(--muted);
      font-size: var(--text-xs);
      font-weight: var(--weight-medium);
    }
    .holdings {
      margin: 0;
      padding: 0;
      list-style: none;
    }
    .holdings li {
      min-height: var(--row-h);
      border-bottom: 1px solid var(--rule);
      font-size: var(--text-md);
    }
    .r {
      text-align: right;
    }
    .ticker {
      font-weight: var(--weight-bold);
      overflow-wrap: anywhere;
    }
    .weight {
      display: flex;
      align-items: center;
      justify-content: flex-end;
      gap: var(--space-2);
    }
    .bar {
      min-width: 2px;
      height: 4px;
      border-radius: 2px;
      background: var(--accent);
    }
    .more {
      display: inline-flex;
      align-items: center;
      min-height: var(--touch-min);
      font-size: var(--text-md);
    }
    /* Tablets and below: weight and last price step aside. */
    @include bp.below-desktop {
      .grid {
        grid-template-columns: minmax(0, 1fr) 4.5rem 7.5rem 5rem;
      }
      .wide {
        display: none;
      }
    }
    /* Phones: a list item, ticker and quantity left, value and return right. */
    @include bp.phone {
      .head {
        display: none;
      }
      .holdings li {
        grid-template-columns: minmax(0, 1fr) auto;
        grid-template-areas: 'ticker value' 'qty pnl';
        gap: 2px var(--space-3);
        padding: var(--space-3) 0;
      }
      .ticker {
        grid-area: ticker;
      }
      .qty {
        grid-area: qty;
        text-align: left;
        color: var(--muted);
        font-size: var(--text-sm);
      }
      .value {
        grid-area: value;
        font-size: var(--text-lead);
      }
      .pnl {
        grid-area: pnl;
        font-size: var(--text-sm);
      }
    }
  `,
})
export class HoldingsSection {
  private readonly store = inject(HomeStore);

  constructor() {
    this.store.need('portfolio');
  }

  protected readonly book = computed(() =>
    this.store.portfolio.hasValue() ? this.store.portfolio.value() : null,
  );
  private readonly sorted = computed(() =>
    [...(this.book()?.positions ?? [])].sort(
      (a, b) => (b.market_value ?? 0) - (a.market_value ?? 0),
    ),
  );
  protected readonly count = computed(() => this.sorted().length);
  private readonly maxWeight = computed(() =>
    Math.max(0, ...this.sorted().map((p) => Math.abs(p.weight ?? 0))),
  );
  protected readonly holdings = computed(() => {
    const currency = this.book()?.currency ?? undefined;
    const top = this.maxWeight() || 1;
    return this.sorted()
      .slice(0, TOP_HOLDINGS)
      .map((p) => ({
        ticker: p.ticker,
        quantity: `${formatNumber(p.quantity, { digits: 2 })} shares`,
        price: p.price == null ? '' : formatMoney(p.price, { currency: p.currency ?? currency }),
        value:
          p.market_value == null
            ? 'n/a'
            : formatMoney(p.market_value, { currency: p.currency ?? currency }),
        pnl:
          p.unrealized_pnl_pct == null ? '' : formatPercent(p.unrealized_pnl_pct, { signed: true }),
        tone: toneClass(p.unrealized_pnl_pct),
        weight: p.weight,
        weightText: p.weight == null ? '' : formatPercent(p.weight, { digits: 0 }),
        bar: p.weight == null ? 0 : (Math.abs(p.weight) / top) * 100,
      }));
  });
  protected readonly more = computed(() => Math.max(0, this.sorted().length - TOP_HOLDINGS));
}
