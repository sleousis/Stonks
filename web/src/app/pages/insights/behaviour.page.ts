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
    <app-page-header
      title="Behaviour"
      description="How you trade by hand: your manual orders and synced broker trades."
    />
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
      <div class="page-grid">
        <section class="panel span-12" aria-labelledby="bh-summary">
          <div class="panel-head"><h2 id="bh-summary">Summary</h2></div>
          <div class="panel-body">
            <dl class="stats">
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
              <ul class="findings">
                @for (n of notes(); track n) {
                  <li>{{ n }}</li>
                }
              </ul>
            }
          </div>
        </section>

        <section class="panel span-6" aria-labelledby="bh-holding">
          <div class="panel-head"><h2 id="bh-holding">By holding time</h2></div>
          <div class="panel-body">
            <ng-container
              *ngTemplateOutlet="table; context: { rows: r.by_holding, caption: 'By holding time' }"
            />
          </div>
        </section>

        <section class="panel span-6" aria-labelledby="bh-weekday">
          <div class="panel-head"><h2 id="bh-weekday">By weekday of entry</h2></div>
          <div class="panel-body">
            <ng-container
              *ngTemplateOutlet="table; context: { rows: weekdays(), caption: 'By weekday' }"
            />
          </div>
        </section>

        <section class="panel span-6" aria-labelledby="bh-habits">
          <div class="panel-head"><h2 id="bh-habits">Habits</h2></div>
          <div class="panel-body">
            <dl class="stats">
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
                <dt>Busy days vs the rest</dt>
                <dd class="num">
                  {{ money(r.overtrading.busy_day_pnl) }} / {{ money(r.overtrading.other_day_pnl) }}
                </dd>
              </div>
              <div>
                <dt>Revenge trades</dt>
                <dd class="num" [class]="tone(r.revenge.pnl)">
                  {{ num(r.revenge.trades) }}, {{ money(r.revenge.pnl) }}
                </dd>
              </div>
            </dl>
          </div>
        </section>

        <section class="panel span-6" aria-labelledby="bh-strategies">
          <div class="panel-head"><h2 id="bh-strategies">Against the strategies</h2></div>
          <div class="panel-body">
            <p class="muted">
              Each entry next to the active strategies' signals in the week before it.
            </p>
            <ng-container
              *ngTemplateOutlet="table; context: { rows: stances(), caption: 'Versus strategies' }"
            />
          </div>
        </section>
      </div>
    }

    <ng-template #table let-rows="rows" let-caption="caption">
      <table class="grid">
        <caption class="visually-hidden">
          {{
            caption
          }}
        </caption>
        <thead>
          <tr>
            <th scope="col"></th>
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

    .stats {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(10rem, 1fr));
      gap: var(--space-3);
    }
    .stats dt {
      color: var(--color-ink-2);
      font-size: var(--text-sm);
    }
    .stats dd {
      font-size: var(--text-lg);
    }
    .findings {
      margin-top: var(--space-3);
      padding-left: var(--space-4);
      display: grid;
      gap: var(--space-1);
    }
    .grid {
      width: 100%;
      border-collapse: collapse;
    }
    .grid th,
    .grid td {
      padding: var(--space-2);
      border-bottom: 1px solid var(--color-border);
      text-align: left;
    }
    .grid .num {
      text-align: right;
    }
    .gain {
      color: var(--color-gain);
    }
    .loss {
      color: var(--color-loss);
    }
    @include bp.phone {
      .grid thead {
        position: absolute;
        width: 1px;
        height: 1px;
        overflow: hidden;
        clip: rect(0 0 0 0);
      }
      .grid tr {
        display: grid;
        grid-template-columns: 1fr 1fr;
        padding: var(--space-2) 0;
        border-bottom: 1px solid var(--color-border);
      }
      .grid th[scope='row'] {
        grid-column: 1 / -1;
      }
      .grid th,
      .grid td {
        border: 0;
        padding: var(--space-1) 0;
      }
      .grid td::before {
        content: attr(data-label) ': ';
        color: var(--color-ink-2);
      }
      .grid .num {
        text-align: left;
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
