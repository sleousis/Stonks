import { NgTemplateOutlet } from '@angular/common';
import { ChangeDetectionStrategy, Component, computed, inject, resource } from '@angular/core';

import { InsightsService } from '../../api/insights.service';
import type { BehaviourBucketView, BehaviourView } from '../../api/models';
import { formatMoney, formatNumber, formatPercent, toneClass } from '../../core/format/format';
import { PortfolioContextService } from '../../core/portfolio/portfolio-context.service';
import { NoBook, bookState } from '../../shared/ui/no-book';
import { PageHeader } from '../../shared/ui/page-header';
import { EmptyState, ErrorState, LoadingState } from '../../shared/ui/states';
import { InsightsNav } from './insights-nav';

const STANCE_LABELS: Record<string, string> = {
  with: 'With the strategies',
  against: 'Against the strategies',
  no_view: 'No strategy view',
};

/** Plain findings from a behaviour report, most telling first. */
export function findings(r: BehaviourView, currency = 'USD'): string[] {
  const out: string[] = [];
  const money = (v: number | null | undefined) => formatMoney(v, { currency });
  const d = r.disposition;
  if (d.present && d.ratio) {
    out.push(`You hold losers ${formatNumber(d.ratio, { digits: 1 })} times longer than winners.`);
  }
  if (r.revenge.trades > 0) {
    out.push(
      `${formatNumber(r.revenge.trades)} trades came within a day of a losing exit and made ` +
        `${money(r.revenge.pnl)} together.`,
    );
  }
  if (r.overtrading.busy_days > 0) {
    out.push(
      `${formatNumber(r.overtrading.busy_days)} busy days (5 or more entries) made ` +
        `${money(r.overtrading.busy_day_pnl)}.`,
    );
  }
  const cost = r.against_strategies_cost;
  const against = r.versus_strategies.find((b) => b.label === 'against');
  if (cost !== null && cost !== undefined && against && against.trades > 0) {
    out.push(
      `${formatNumber(against.trades)} trades went against the active strategies and made ` +
        `${money(cost)}.`,
    );
  }
  return out;
}

/**
 * How you trade by hand in the picked portfolio: your manual orders and the
 * trades a broker sync brought in, paired into round trips. P&L by holding
 * time and weekday, the disposition effect, overtrading, revenge trades
 * after a loss, and what trading against the active strategies cost.
 */
@Component({
  selector: 'app-behaviour-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    NgTemplateOutlet,
    PageHeader,
    InsightsNav,
    NoBook,
    LoadingState,
    ErrorState,
    EmptyState,
  ],
  template: `
    <app-page-header title="Behaviour" [description]="standfirst()" />
    <app-insights-nav />

    @if (book() === 'none') {
      <app-no-book message="Your trading habits show here once you have a portfolio." />
    } @else if (report.error(); as err) {
      <app-error-state
        title="Could not load the behaviour report"
        [error]="err"
        (retry)="report.reload()"
      />
    } @else if (!report.hasValue()) {
      <app-loading-state label="Loading the behaviour report" [rows]="4" />
    } @else if (report.value().trades === 0) {
      <app-empty-state
        title="No closed trades yet"
        message="Once you buy and sell by hand, or a broker sync brings trades in, your habits show here."
      />
    } @else {
      @let r = report.value();
      <section aria-labelledby="bh-summary">
        <h2 id="bh-summary" class="visually-hidden">Summary</h2>
        <dl class="summary-strip stats strip">
          <div>
            <dt>Closed trades</dt>
            <dd class="num">{{ num(r.trades) }}</dd>
          </div>
          <div>
            <dt>Win rate</dt>
            <dd class="num">{{ pct(r.win_rate) }}</dd>
          </div>
          <div>
            <dt>Profit and loss</dt>
            <dd class="num" [class]="tone(r.total_pnl)">{{ money(r.total_pnl) }}</dd>
          </div>
          <div>
            <dt>Average win / loss</dt>
            <dd class="num">{{ money(r.avg_win) }} / {{ money(r.avg_loss) }}</dd>
          </div>
        </dl>
        @if (notes().length) {
          <div class="callout findings-box">
            <p class="callout-title">What stands out</p>
            <ul class="findings">
              @for (n of notes(); track n) {
                <li>{{ n }}</li>
              }
            </ul>
          </div>
        }
      </section>

      <div class="cols body">
        <div class="section-stack">
          <section class="page-section section-flush" aria-labelledby="bh-holding">
            <header class="section-head"><h2 id="bh-holding">By holding time</h2></header>
            <ng-container
              *ngTemplateOutlet="table; context: { rows: r.by_holding, caption: 'By holding time' }"
            />
          </section>

          <section class="page-section section-flush" aria-labelledby="bh-weekday">
            <header class="section-head"><h2 id="bh-weekday">By weekday of entry</h2></header>
            <ng-container
              *ngTemplateOutlet="table; context: { rows: weekdays(), caption: 'By weekday' }"
            />
          </section>

          <section class="page-section section-flush" aria-labelledby="bh-strategies">
            <header class="section-head"><h2 id="bh-strategies">Against the strategies</h2></header>
            <p class="table-note muted">
              Each entry next to the active strategies' signals in the week before it.
            </p>
            <ng-container
              *ngTemplateOutlet="table; context: { rows: stances(), caption: 'Versus strategies' }"
            />
          </section>
        </div>

        <section class="page-section" aria-labelledby="bh-habits">
          <header class="section-head"><h2 id="bh-habits">Habits</h2></header>
          <dl class="stats habits">
            <div>
              <dt>Days held, winners</dt>
              <dd class="num">{{ days(r.disposition.avg_days_winners) }}</dd>
            </div>
            <div>
              <dt>Days held, losers</dt>
              <dd class="num">{{ days(r.disposition.avg_days_losers) }}</dd>
            </div>
            <div>
              <dt>Entries a trading day</dt>
              <dd class="num">{{ days(r.overtrading.entries_per_active_day) }}</dd>
            </div>
            <div>
              <dt>Most in a day</dt>
              <dd class="num">{{ num(r.overtrading.max_entries_in_a_day) }}</dd>
            </div>
            <div>
              <dt>Busy days</dt>
              <dd class="num">{{ num(r.overtrading.busy_days) }}</dd>
            </div>
            <div>
              <dt>P&amp;L on busy days</dt>
              <dd class="num" [class]="tone(r.overtrading.busy_day_pnl)">
                {{ money(r.overtrading.busy_day_pnl) }}
              </dd>
            </div>
            <div>
              <dt>P&amp;L on other days</dt>
              <dd class="num" [class]="tone(r.overtrading.other_day_pnl)">
                {{ money(r.overtrading.other_day_pnl) }}
              </dd>
            </div>
            <div>
              <dt>Revenge trades</dt>
              <dd class="num">{{ num(r.revenge.trades) }}</dd>
            </div>
            <div>
              <dt>P&amp;L from revenge trades</dt>
              <dd class="num" [class]="tone(r.revenge.pnl)">{{ money(r.revenge.pnl) }}</dd>
            </div>
          </dl>
        </section>
      </div>
    }

    <ng-template #table let-rows="rows" let-caption="caption">
      <table class="table grid">
        <caption class="visually-hidden">
          {{
            caption
          }}
        </caption>
        <thead>
          <tr>
            <th scope="col"><span class="visually-hidden">Group</span></th>
            <th scope="col" class="num">Trades</th>
            <th scope="col" class="num">Win rate</th>
            <th scope="col" class="num">P&amp;L</th>
          </tr>
        </thead>
        <tbody>
          @for (b of rows; track b.label) {
            <tr>
              <th scope="row">{{ b.label }}</th>
              <td data-label="Trades" class="num">{{ num(b.trades) }}</td>
              <td data-label="Win rate" class="num">{{ pct(b.win_rate) }}</td>
              <td data-label="P&L" class="num" [class]="tone(b.pnl)">{{ money(b.pnl) }}</td>
            </tr>
          }
        </tbody>
      </table>
    </ng-template>
  `,
  styles: `
    @use 'breakpoints' as bp;

    /* The summary: label over a mono figure, hairlines between the cells. */
    .strip {
      margin: 0;

      > div {
        padding: var(--space-3) var(--space-4) var(--space-4) 0;
      }

      > div + div {
        padding-left: var(--space-4);
      }
    }
    .stats dt {
      color: var(--muted);
      font-size: var(--text-xs);
      font-weight: var(--weight-medium);
    }
    .stats dd {
      margin: var(--space-1) 0 0;
      font-size: var(--text-stat);
      font-weight: var(--weight-semibold);
      letter-spacing: -0.03em;
      white-space: normal;
      overflow-wrap: anywhere;
    }
    .findings-box {
      margin-top: var(--space-5);
    }
    .findings {
      display: grid;
      gap: var(--space-1);
      margin: var(--space-2) 0 0;
      padding-left: var(--space-5);
    }
    .body {
      margin-top: var(--section-gap);
    }
    /* Habits: fact rows, the label left and the figure right. */
    .habits {
      margin: 0;

      > div {
        display: flex;
        flex-wrap: wrap;
        align-items: baseline;
        justify-content: space-between;
        gap: var(--space-1) var(--space-4);
        padding: 11px 0;
        border-bottom: 1px solid var(--rule);
      }

      > div:last-child {
        border-bottom: 0;
      }

      dt {
        color: var(--ink-2);
        font-size: var(--text-md);
        font-weight: var(--weight-regular);
      }

      dd {
        margin: 0;
        font-size: var(--text-lead);
        text-align: right;
      }
    }
    .table-note {
      margin: 0;
      padding: var(--space-3) 0;
      border-bottom: 1px solid var(--rule);
      font-size: var(--text-sm);
    }
    .grid tbody th {
      font-weight: var(--weight-semibold);
      text-align: left;
    }
    @include bp.phone {
      .strip > div,
      .strip > div + div {
        padding: var(--space-3) var(--space-3) var(--space-3) 0;
      }
      .strip > div:nth-child(even) {
        padding-left: var(--space-3);
      }
      .grid thead {
        position: absolute;
        width: 1px;
        height: 1px;
        overflow: hidden;
        clip: rect(0 0 0 0);
      }
      .grid tr {
        display: grid;
        grid-template-columns: repeat(3, minmax(0, 1fr));
        gap: var(--space-1) var(--space-3);
        padding: var(--space-3) 0;
        border-bottom: 1px solid var(--rule);
      }
      .grid th[scope='row'] {
        grid-column: 1 / -1;
      }
      .grid th,
      .grid td {
        height: auto;
        padding: 0;
        border: 0;
      }
      .grid td {
        display: grid;
        gap: 2px;
        text-align: left;
      }
      .grid td::before {
        content: attr(data-label);
        color: var(--muted);
        font-family: var(--font-sans);
        font-size: var(--text-xs);
      }
    }
  `,
})
export class BehaviourPage {
  private readonly api = inject(InsightsService);
  private readonly ctx = inject(PortfolioContextService);

  protected readonly book = computed(() => bookState(this.ctx));

  protected readonly report = resource({
    params: () => (this.book() === 'ready' ? { portfolio: this.ctx.selectedId() } : undefined),
    loader: () => this.api.behaviour(),
  });

  protected readonly notes = computed(() =>
    this.report.hasValue() ? findings(this.report.value(), this.currency()) : [],
  );
  /** Weekdays with trades (weekends only when you traded then). */
  protected readonly weekdays = computed<BehaviourBucketView[]>(() =>
    this.report.hasValue()
      ? this.report.value().by_weekday.filter((b, i) => i < 5 || b.trades > 0)
      : [],
  );
  protected readonly stances = computed<BehaviourBucketView[]>(() =>
    this.report.hasValue()
      ? this.report
          .value()
          .versus_strategies.map((b) => ({ ...b, label: STANCE_LABELS[b.label] ?? b.label }))
      : [],
  );

  /** "24 closed trades, 58% of them winners." The default while it loads. */
  protected readonly standfirst = computed(() => {
    if (!this.report.hasValue() || !this.report.value().trades) {
      return 'How you trade by hand: your manual orders and synced broker trades.';
    }
    const r = this.report.value();
    const trades = `${this.num(r.trades)} closed ${r.trades === 1 ? 'trade' : 'trades'}`;
    return r.win_rate == null
      ? `${trades} by hand or from your broker.`
      : `${trades}, ${this.pct(r.win_rate)} of them winners.`;
  });

  protected readonly tone = toneClass;
  protected num(v: number): string {
    return formatNumber(v);
  }
  protected pct(v: number | null | undefined): string {
    return formatPercent(v ?? null, { digits: 0 });
  }
  private readonly currency = computed(() => this.ctx.current()?.base_currency ?? 'USD');
  protected money(v: number | null | undefined): string {
    return formatMoney(v ?? null, { currency: this.currency() });
  }
  protected days(v: number | null | undefined): string {
    return formatNumber(v ?? null, { digits: 1 });
  }
}
