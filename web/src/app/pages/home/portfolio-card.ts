import { ChangeDetectionStrategy, Component, computed, inject, resource } from '@angular/core';
import { RouterLink } from '@angular/router';

import { PortfolioService } from '../../api/portfolio.service';
import { formatMoney, formatNumber, formatPercent, toneClass } from '../../core/format/format';
import { StatTile } from '../../shared/ui/stat-tile';
import { EmptyState, ErrorState, LoadingState } from '../../shared/ui/states';

const TOP_HOLDINGS = 8;

/** My portfolio: value, today's change and the biggest holdings. */
@Component({
  selector: 'app-portfolio-card',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink, StatTile, LoadingState, EmptyState, ErrorState],
  template: `
    <section class="panel" aria-labelledby="home-portfolio">
      <div class="panel-head">
        <h2 id="home-portfolio">My portfolio</h2>
        <a routerLink="/dashboard" class="more">Details</a>
      </div>
      @if (portfolio.error(); as err) {
        <app-error-state
          title="Could not load your portfolio"
          [error]="err"
          (retry)="portfolio.reload()"
        />
      } @else if (!portfolio.hasValue()) {
        <app-loading-state label="Loading your portfolio" [rows]="4" />
      } @else {
        <div class="panel-body body">
          <div class="tiles">
            <app-stat-tile label="Value" featured [value]="value()" [help]="false" />
            <app-stat-tile
              label="Today"
              [value]="dayChange() ?? 'No change yet'"
              [detail]="dayReturn()"
              [detailTone]="dayTone()"
              [help]="false"
            />
          </div>
          @if (holdings().length === 0) {
            <app-empty-state
              title="No holdings yet"
              message="Holdings show here after your first paper or auto trade."
            />
          } @else {
            <ul class="holdings" aria-label="Holdings">
              @for (h of holdings(); track h.ticker) {
                <li>
                  <span class="ticker">{{ h.ticker }}</span>
                  <span class="qty num muted">{{ h.quantity }}</span>
                  <span class="value num">{{ h.value }}</span>
                  <span class="pnl num" [class]="h.tone">{{ h.pnl }}</span>
                </li>
              }
            </ul>
            @if (more() > 0) {
              <a routerLink="/dashboard" class="more">and {{ more() }} more</a>
            }
          }
        </div>
      }
    </section>
  `,
  styles: `
    :host {
      display: block;
      min-width: 0;
    }
    .body {
      display: grid;
      gap: var(--space-4);
    }
    .tiles {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: var(--space-3);
    }
    .more {
      display: inline-flex;
      align-items: center;
      min-height: var(--touch-min);
      font-size: var(--text-sm);
    }
    .holdings {
      display: grid;
      margin: 0;
      padding: 0;
      list-style: none;
    }
    .holdings li {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto auto;
      grid-template-areas: 'ticker value pnl' 'qty value pnl';
      column-gap: var(--space-3);
      align-items: center;
      padding: var(--space-2) 0;
      border-bottom: 1px solid var(--color-border);
    }
    .holdings li:last-child {
      border-bottom: 0;
    }
    .ticker {
      grid-area: ticker;
      font-weight: var(--weight-semibold);
      overflow-wrap: anywhere;
    }
    .qty {
      grid-area: qty;
      font-size: var(--text-xs);
    }
    .value {
      grid-area: value;
      text-align: right;
    }
    .pnl {
      grid-area: pnl;
      min-width: 4.5rem;
      text-align: right;
      font-size: var(--text-sm);
    }
  `,
})
export class PortfolioCard {
  private readonly api = inject(PortfolioService);

  protected readonly portfolio = resource({ loader: () => this.api.get() });
  protected readonly pnl = resource({ loader: () => this.api.pnl() });

  private readonly latest = computed(() =>
    this.pnl.hasValue() ? (this.pnl.value().rows.at(-1) ?? null) : null,
  );

  protected readonly value = computed(() =>
    this.portfolio.hasValue() ? formatMoney(this.portfolio.value().total_value) : '',
  );
  protected readonly dayChange = computed(() => {
    const row = this.latest();
    return row?.daily_change == null ? null : formatMoney(row.daily_change, { signed: true });
  });
  protected readonly dayReturn = computed(() => {
    const row = this.latest();
    return row?.daily_return == null ? null : formatPercent(row.daily_return, { signed: true });
  });
  protected readonly dayTone = computed(() => toneClass(this.latest()?.daily_change));

  private readonly sorted = computed(() =>
    this.portfolio.hasValue()
      ? [...this.portfolio.value().positions].sort(
          (a, b) => (b.market_value ?? 0) - (a.market_value ?? 0),
        )
      : [],
  );
  protected readonly holdings = computed(() =>
    this.sorted()
      .slice(0, TOP_HOLDINGS)
      .map((p) => ({
        ticker: p.ticker,
        quantity: `${formatNumber(p.quantity)} shares`,
        value: p.market_value == null ? 'n/a' : formatMoney(p.market_value),
        pnl:
          p.unrealized_pnl_pct == null ? '' : formatPercent(p.unrealized_pnl_pct, { signed: true }),
        tone: toneClass(p.unrealized_pnl_pct),
      })),
  );
  protected readonly more = computed(() => Math.max(0, this.sorted().length - TOP_HOLDINGS));
}
