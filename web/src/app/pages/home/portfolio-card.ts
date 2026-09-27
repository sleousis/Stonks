import { ChangeDetectionStrategy, Component, computed, inject, resource } from '@angular/core';
import { RouterLink } from '@angular/router';

import { PortfolioService } from '../../api/portfolio.service';
import {
  activeFormat,
  formatMoney,
  formatNumber,
  formatPercent,
  toneClass,
} from '../../core/format/format';
import { ModeStamp } from '../../shared/ui/mode-stamp';
import { StatTile } from '../../shared/ui/stat-tile';
import { autoRefresh } from '../../shared/auto-refresh';
import { NoBook } from '../../shared/ui/no-book';
import { EmptyState, ErrorState, LoadingState } from '../../shared/ui/states';
import { PortfolioContextService } from '../../core/portfolio/portfolio-context.service';
import { RunsPassed } from './runs-passed';

const TOP_HOLDINGS = 8;
const DAY_MS = 24 * 60 * 60 * 1000;

/**
 * The label of the day's-change tile (UX-34): "Today" for today's session,
 * the weekday ("Friday") for one earlier this week, else "Last session".
 * `day` is the P&L row's date (YYYY-MM-DD).
 */
export function sessionLabel(day: string | null | undefined, now = new Date()): string {
  if (!day) return 'Today';
  const { locale, timeZone } = activeFormat();
  const today = new Intl.DateTimeFormat('en-CA', { timeZone }).format(now);
  if (day === today) return 'Today';
  const at = Date.parse(`${day}T00:00:00Z`);
  const days = Math.round((Date.parse(`${today}T00:00:00Z`) - at) / DAY_MS);
  if (!Number.isFinite(days) || days < 0 || days > 6) return 'Last session';
  return new Intl.DateTimeFormat(locale, { weekday: 'long', timeZone: 'UTC' }).format(at);
}

/** My portfolio: value, today's change and the biggest holdings. */
@Component({
  selector: 'app-portfolio-card',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink, StatTile, ModeStamp, NoBook, LoadingState, EmptyState, ErrorState],
  template: `
    <section class="panel" [class.live-frame]="live()" aria-labelledby="home-portfolio">
      <div class="panel-head">
        <h2 id="home-portfolio">My portfolio</h2>
        <span class="head-end">
          @if (mode(); as m) {
            <app-mode-stamp [live]="m === 'live'" />
          }
          @if (hasBook()) {
            <a routerLink="/insights" class="more">Details</a>
          }
        </span>
      </div>
      @if (portfolio.error(); as err) {
        <app-error-state
          title="Could not load your portfolio"
          [error]="err"
          (retry)="portfolio.reload()"
        />
      } @else if (!portfolio.hasValue()) {
        <app-loading-state label="Loading your portfolio" [rows]="4" />
      } @else if (portfolio.value() === null) {
        <app-no-book
          message="You get signals from the strategies you follow. A paper portfolio lets them trade for you with pretend money."
        />
      } @else {
        <div class="panel-body body">
          <div class="tiles">
            <app-stat-tile
              label="Value"
              featured
              [live]="live()"
              [value]="value()"
              [amount]="total()"
              [format]="money"
              [help]="false"
            />
            <app-stat-tile
              [label]="dayLabel()"
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
            >
              <a routerLink="/orders" class="btn">See orders</a>
            </app-empty-state>
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
              <a routerLink="/insights" class="more">and {{ more() }} more</a>
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
    /* Phones: the headline figure gets the whole width. */
    @media (max-width: 767.98px) {
      .tiles .featured {
        grid-column: 1 / -1;
      }
    }
    .head-end {
      display: inline-flex;
      align-items: center;
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

  private readonly portfolioCtx = inject(PortfolioContextService);

  /**
   * Null when the person has no portfolio (they follow strategies for
   * signals only): then nothing asks for holdings, so no 404 (BUG-3).
   */
  protected readonly portfolio = resource({
    params: () => ({ portfolio: this.portfolioCtx.selectedId() }),
    loader: () => this.ifBook(() => this.api.get()),
  });
  protected readonly pnl = resource({
    params: () => ({ portfolio: this.portfolioCtx.selectedId() }),
    loader: () => this.ifBook(() => this.api.pnl()),
  });

  private async ifBook<T>(read: () => Promise<T>): Promise<T | null> {
    await this.portfolioCtx.load();
    if (this.portfolioCtx.noBook()) return null;
    return read();
  }

  /** The value moves during the session: every minute, and when a trading run starts (UX-12). */
  protected readonly auto = autoRefresh(() => [this.portfolio, this.pnl], {
    triggers: [inject(RunsPassed).count],
  });

  /** "Details" only when there is a portfolio to show details of. */
  protected readonly hasBook = computed(
    () => this.portfolio.hasValue() && this.portfolio.value() !== null,
  );

  private readonly latest = computed(() =>
    this.pnl.hasValue() ? (this.pnl.value()?.rows.at(-1) ?? null) : null,
  );

  protected readonly value = computed(() => {
    const book = this.portfolio.hasValue() ? this.portfolio.value() : null;
    return book ? formatMoney(book.total_value) : '';
  });
  protected readonly total = computed(() => {
    const book = this.portfolio.hasValue() ? this.portfolio.value() : null;
    return book ? book.total_value : null;
  });
  protected readonly money = (n: number) => formatMoney(n);
  /** Paper or live, when the portfolio list is known. Brass means live. */
  protected readonly mode = computed(() => this.portfolioCtx.current()?.trading ?? null);
  protected readonly live = computed(() => this.mode() === 'live');
  protected readonly dayChange = computed(() => {
    const row = this.latest();
    return row?.daily_change == null ? null : formatMoney(row.daily_change, { signed: true });
  });
  protected readonly dayReturn = computed(() => {
    const row = this.latest();
    return row?.daily_return == null ? null : formatPercent(row.daily_return, { signed: true });
  });
  protected readonly dayTone = computed(() => toneClass(this.latest()?.daily_change));
  protected readonly dayLabel = computed(() => sessionLabel(this.latest()?.day));

  private readonly sorted = computed(() =>
    this.portfolio.hasValue() && this.portfolio.value()
      ? [...this.portfolio.value()!.positions].sort(
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
