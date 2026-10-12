import { ChangeDetectionStrategy, Component, computed, inject, signal } from '@angular/core';
import { RouterLink } from '@angular/router';

import { formatMoney, formatNumber, formatPercent, toneClass } from '../../core/format/format';
import {
  dayChangeFrom,
  dayChangeMoney,
  dayChangePercent,
  sessionPhrase,
} from '../../core/format/day-change';
import { PortfolioContextService } from '../../core/portfolio/portfolio-context.service';
import { TradingDayService } from '../../core/schedule/trading-day.service';
import { baseCurrencyLine } from '../../shared/base-currency';
import { autoRefresh } from '../../shared/auto-refresh';
import { countUp } from '../../shared/ui/count-up';
import { ModeStamp } from '../../shared/ui/mode-stamp';
import { NoBook } from '../../shared/ui/no-book';
import { isReadOnly } from '../../shared/ui/portfolio-picker';
import { type SegmentOption, Segmented } from '../../shared/ui/segmented';
import { ErrorState, LoadingState } from '../../shared/ui/states';
import { type EquityPoint, EquityArea } from './equity-area';
import { HomeStore } from './home-store';

/** A yearly rate means little before about three months of history (NB-14). */
const YEARLY_AFTER_DAYS = 90;
const DAY_MS = 86_400_000;
/** The meter draws one tick per paper day up to this many. */
const METER_MAX_TICKS = 30;

export type Range = '1W' | '1M' | '3M' | '1Y' | 'All';
const RANGE_DAYS: Record<Range, number | null> = {
  '1W': 7,
  '1M': 30,
  '3M': 91,
  '1Y': 365,
  All: null,
};

/** Calendar days between the first and last day of a P&L series. */
export function historyDays(rows: readonly { day: string }[]): number {
  if (rows.length < 2) return 0;
  return (Date.parse(rows.at(-1)!.day) - Date.parse(rows[0].day)) / DAY_MS;
}
/** Moved to core/format/day-change.ts so every page names the day alike (M2). */
export { sessionLabel } from '../../core/format/day-change';

/** The rows within `range` of the last day. */
export function inRange<T extends { day: string }>(rows: readonly T[], range: Range): T[] {
  const days = RANGE_DAYS[range];
  if (days === null || rows.length === 0) return [...rows];
  const from = Date.parse(rows.at(-1)!.day) - days * DAY_MS;
  return rows.filter((r) => Date.parse(r.day) >= from);
}

/**
 * A money figure split for the headline: the whole part, then the cents and
 * any currency after them, which the headline sets in the muted ink.
 * "$10,412.37" gives ["$10,412", ".37"]. A masked or odd figure stays whole.
 */
export function splitMoney(text: string): [string, string] {
  const m = /^(.*\d)([.,]\d{1,2})(\D*)$/.exec(text);
  return m ? [m[1], m[2] + m[3]] : [text, ''];
}

/**
 * My portfolio, the overview at the top of Today: the value as the one big
 * figure, the last session's change, the value curve with a range picker,
 * and beside it the facts (cash, invested, open orders, worst drop) and the
 * paper-days meter. Live portfolios draw the figure and curve in brass.
 */
@Component({
  selector: 'app-portfolio-card',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink, ModeStamp, NoBook, LoadingState, ErrorState, Segmented, EquityArea],
  template: `
    <section class="overview" aria-labelledby="home-portfolio">
      <div class="main">
        <header class="head">
          <h2 id="home-portfolio">My portfolio</h2>
          @if (mode(); as m) {
            <app-mode-stamp [live]="m === 'live'" [stage]="stage()" [readOnly]="readOnly()" />
          }
          @if (hasBook()) {
            <a routerLink="/insights" class="text-link details">Details</a>
          }
        </header>
        @if (store.portfolio.error(); as err) {
          <app-error-state
            title="Could not load your portfolio"
            [error]="err"
            (retry)="store.portfolio.reload()"
          />
        } @else if (!store.portfolio.hasValue()) {
          <app-loading-state label="Loading your portfolio" [rows]="4" />
        } @else if (store.portfolio.value() === null) {
          <app-no-book
            message="You get signals from the strategies you follow. A paper portfolio lets them trade for you with pretend money."
          />
        } @else {
          <p class="headline-figure value" [class.brass]="live()">
            <span class="visually-hidden">Value </span>{{ figure()[0]
            }}<span class="minor">{{ figure()[1] }}</span>
          </p>
          @if (baseLine(); as line) {
            <p class="base">{{ line }}</p>
          }
          <p class="change">
            @if (dayChange(); as c) {
              <span class="pill num" [attr.data-tone]="dayTone()">{{ c }}</span>
              @if (dayReturn(); as r) {
                <span class="num" [class]="dayTone()">{{ r }}</span>
              }
              <span class="when">{{ dayPhrase() }}</span>
            } @else {
              <span class="when">No change yet</span>
            }
          </p>
          @if (returnsLine(); as line) {
            <p class="returns">{{ line }}</p>
          }
          @if (curve().length > 1) {
            <div class="curve">
              <app-segmented
                class="range"
                label="Range"
                emphasis="strong"
                [options]="ranges"
                [(value)]="range"
              />
              <app-equity-area [points]="curve()" [live]="live()" [summary]="curveSummary()" />
            </div>
          } @else {
            <p class="hint">The value curve starts once the portfolio has two days of history.</p>
          }
        }
      </div>

      @if (hasBook()) {
        <aside class="facts" aria-label="Portfolio facts">
          <dl class="fact-list">
            <div>
              <dt>Cash</dt>
              <dd>{{ cash() }}</dd>
            </div>
            <div>
              <dt>Invested</dt>
              <dd>{{ invested() }}</dd>
            </div>
            <div>
              <dt>Open orders</dt>
              <dd>{{ openOrders() }}</dd>
            </div>
            <div>
              <dt>Worst drop</dt>
              <dd [class.loss]="worstDropValue() < 0">{{ worstDrop() }}</dd>
            </div>
          </dl>
          @if (meter(); as m) {
            <div class="meter">
              <p class="meter-head">
                <span>Paper days</span>
                <span class="num">{{ m.done }} of {{ m.required }}</span>
              </p>
              @if (m.ticks.length) {
                <ol class="ticks" aria-hidden="true">
                  @for (t of m.ticks; track $index) {
                    <li [class.on]="t"></li>
                  }
                </ol>
              }
              <p class="meter-text">{{ m.text }}</p>
            </div>
          }
        </aside>
      }
    </section>
  `,
  styles: `
    @use 'breakpoints' as bp;
    :host {
      display: block;
      min-width: 0;
    }
    /* The figure and curve, with a 320px facts column from desktop. */
    .overview {
      display: grid;
      gap: var(--space-6) var(--column-gap);
      grid-template-columns: minmax(0, 1fr);

      @include bp.from-desktop {
        grid-template-columns: minmax(0, 1fr) 320px;
      }
    }
    .main {
      min-width: 0;
    }
    .head {
      display: flex;
      align-items: center;
      gap: var(--space-2) var(--space-3);
      min-height: var(--touch-min);
    }
    h2 {
      font-size: var(--text-lead);
      font-weight: var(--weight-semibold);
    }
    .details {
      margin-left: auto;
      font-size: var(--text-md);
    }
    .value {
      margin-top: var(--space-1);

      @include bp.phone {
        font-size: clamp(1.75rem, 9.5vw, 2.5rem);
      }
    }
    .base,
    .hint {
      margin-top: var(--space-1);
      color: var(--muted);
      font-size: var(--text-sm);
    }
    .change {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: var(--space-2) var(--space-3);
      margin-top: var(--space-3);
      font-size: var(--text-md);
    }
    .pill {
      padding: 2px var(--space-2);
      border-radius: var(--r-pill);
      background: var(--sunk);
      font-weight: var(--weight-semibold);
    }
    .pill[data-tone='gain'] {
      background: var(--up-soft);
      color: var(--up);
    }
    .pill[data-tone='loss'] {
      background: var(--down-soft);
      color: var(--down);
    }
    .when,
    .returns {
      color: var(--ink-2);
    }
    .returns {
      margin-top: var(--space-1);
      font-size: var(--text-sm);
    }
    .curve {
      display: grid;
      margin-top: var(--space-4);
    }
    .range {
      justify-self: end;
      margin-bottom: var(--space-3);

      @include bp.phone {
        justify-self: stretch;
      }
    }
    /* The range labels are short: all five stay on one line on a phone. */
    @include bp.phone {
      .range ::ng-deep .segmented[data-many] {
        display: flex;
      }
    }
    /* Facts: rows on hairlines, then the paper-days meter. */
    .facts {
      display: grid;
      align-content: start;
      gap: var(--space-5);
      min-width: 0;

      @include bp.from-desktop {
        padding-top: var(--touch-min);
      }
    }
    .meter {
      display: grid;
      gap: var(--space-2);
      padding-top: var(--space-4);
      border-top: 1px solid var(--rule);
    }
    .meter-head {
      display: flex;
      justify-content: space-between;
      font-weight: var(--weight-semibold);
    }
    .ticks {
      display: flex;
      gap: 3px;
      margin: 0;
      padding: 0;
      list-style: none;
    }
    .ticks li {
      flex: 1 1 0;
      max-width: 10px;
      height: 14px;
      border-radius: 2px;
      background: var(--rule);
    }
    .ticks li.on {
      background: var(--accent);
    }
    .meter-text {
      color: var(--ink-2);
      font-size: var(--text-sm);
    }
  `,
})
export class PortfolioCard {
  protected readonly store = inject(HomeStore);
  private readonly portfolioCtx = inject(PortfolioContextService);

  constructor() {
    this.store.need('portfolio', 'orders', 'follows');
  }

  /** The value moves during the session: every minute, and when a trading run starts (UX-12). */
  protected readonly auto = autoRefresh(
    () => [this.store.portfolio, this.store.pnl, this.store.orders],
    { triggers: [inject(TradingDayService).runsPassed] },
  );

  protected readonly ranges: readonly SegmentOption<Range>[] = (
    ['1W', '1M', '3M', '1Y', 'All'] as const
  ).map((r) => ({ value: r, label: r }));
  protected readonly range = signal<Range>('1M');

  private readonly book = computed(() =>
    this.store.portfolio.hasValue() ? this.store.portfolio.value() : null,
  );
  private readonly series = computed(() =>
    this.store.pnl.hasValue() ? this.store.pnl.value() : null,
  );

  /** "Details" and the facts only when there is a portfolio to show. */
  protected readonly hasBook = computed(() => this.book() !== null);

  /** The portfolio's own currency, so the value reads the same here, on Dashboard and on Insights. */
  private readonly currency = computed(() => this.book()?.currency ?? undefined);
  /**
   * The headline figure. A book that reports in its base currency shows the
   * base total: holdings in other currencies are converted into it.
   */
  protected readonly total = computed(() => {
    const book = this.book();
    if (!book) return null;
    const base = book.base_currency ?? this.portfolioCtx.current()?.base_currency;
    return base && book.currency === base && book.total_value_base != null
      ? book.total_value_base
      : book.total_value;
  });
  private readonly shown = countUp(() => this.total());
  protected readonly figure = computed(() => {
    const v = this.shown();
    return splitMoney(v == null ? '' : formatMoney(v, { currency: this.currency() }));
  });
  /** The value in the base currency when it differs, or why it is missing. */
  protected readonly baseLine = computed(() => {
    const book = this.book();
    if (!book) return null;
    return baseCurrencyLine(
      { ...book, currency: book.currency ?? 'USD' },
      book.base_currency ?? this.portfolioCtx.current()?.base_currency,
    );
  });
  /**
   * Since the start, with deposits and withdrawals taken out. The yearly
   * figure waits for about three months of history: ten days of +1.6%
   * would read as +48.5% a year (NB-14).
   */
  protected readonly returnsLine = computed(() => {
    const series = this.series();
    if (!series) return null;
    const yearly = series.mwr != null && historyDays(series.rows) >= YEARLY_AFTER_DAYS;
    if (series.twr == null && !yearly) return null;
    const parts: string[] = [];
    if (series.twr != null) {
      parts.push(`return ${formatPercent(series.twr, { signed: true, digits: 1 })}`);
    }
    if (yearly && series.mwr != null) {
      parts.push(`${formatPercent(series.mwr, { signed: true, digits: 1 })} a year on your money`);
    }
    return `Since the start: ${parts.join(', ')}. Deposits and withdrawals left out.`;
  });
  /** Paper or live, when the portfolio list is known. Brass means live. */
  protected readonly mode = computed(() => this.portfolioCtx.current()?.trading ?? null);
  protected readonly live = computed(() => this.mode() === 'live');
  protected readonly stage = computed(() => this.portfolioCtx.current()?.live_stage ?? null);
  /** A broker account Stonks only reads (a read-only connection or CSV imports): never paper. */
  protected readonly readOnly = computed(() => {
    const p = this.portfolioCtx.current();
    return !!p && isReadOnly(p);
  });
  /** The API's one headline change (`day_change`), the same as Dashboard and Insights (M2). */
  private readonly day = computed(() =>
    dayChangeFrom(this.series()?.day_change, this.series()?.rows.at(-1) ?? null),
  );
  protected readonly dayChange = computed(() => {
    const d = this.day();
    return d?.change == null ? null : dayChangeMoney(d.change, this.currency());
  });
  protected readonly dayReturn = computed(() => {
    const d = this.day();
    return d?.pct == null ? null : dayChangePercent(d.pct);
  });
  protected readonly dayTone = computed(() => toneClass(this.day()?.change));
  protected readonly dayPhrase = computed(() => sessionPhrase(this.day()?.day));

  protected readonly curve = computed<EquityPoint[]>(() =>
    inRange(this.series()?.rows ?? [], this.range()).map((r) => ({
      day: r.day,
      value: r.total_value,
    })),
  );
  protected readonly curveSummary = computed(() => {
    const c = this.curve();
    if (c.length < 2) return '';
    const from = formatMoney(c[0].value, { currency: this.currency() });
    const to = formatMoney(c.at(-1)!.value, { currency: this.currency() });
    return `Portfolio value over ${this.range() === 'All' ? 'all time' : this.range()}: from ${from} to ${to}.`;
  });

  protected readonly cash = computed(() =>
    formatMoney(this.book()?.cash, { currency: this.currency() }),
  );
  protected readonly invested = computed(() =>
    formatMoney(this.book()?.positions_value, { currency: this.currency() }),
  );
  protected readonly openOrders = computed(() => {
    const n = this.store.openOrders();
    return n == null ? '' : formatNumber(n);
  });
  protected readonly worstDropValue = computed(() =>
    Math.min(0, ...(this.series()?.rows ?? []).map((r) => r.drawdown)),
  );
  protected readonly worstDrop = computed(() =>
    formatPercent(this.worstDropValue(), { signed: this.worstDropValue() < 0, digits: 2 }),
  );

  /**
   * Paper days toward a broker account: the furthest follow in this
   * portfolio. None for a live portfolio, which is past that point.
   */
  protected readonly meter = computed(() => {
    if (this.live()) return null;
    const id = this.portfolioCtx.selectedId();
    const subs = this.store
      .follows()
      .filter(
        (s) => s.paper_days_required > 0 && (!id || !s.portfolio_id || s.portfolio_id === id),
      );
    if (!subs.length) return null;
    const best = subs.reduce((a, b) =>
      b.paper_days_completed / b.paper_days_required >
      a.paper_days_completed / a.paper_days_required
        ? b
        : a,
    );
    const required = best.paper_days_required;
    const done = Math.min(best.paper_days_completed, required);
    const ticks =
      required <= METER_MAX_TICKS ? Array.from({ length: required }, (_, i) => i < done) : [];
    const text =
      done >= required
        ? `A strategy has its ${required} paper days. It can trade a broker account now.`
        : `After ${required} paper days you can let a strategy trade a broker account.`;
    return { done, required, ticks, text };
  });
}
