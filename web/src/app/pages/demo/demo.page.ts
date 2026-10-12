import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  resource,
  signal,
} from '@angular/core';
import { RouterLink } from '@angular/router';

import { DemoService } from '../../api/demo.service';
import type { DemoPortfolioView, DemoPositionView } from '../../api/models';
import { ConfirmService } from '../../core/confirm/confirm.service';
import { formatDate, formatMoney, formatPercent, toneClass } from '../../core/format/format';
import type { ChartSeries } from '../../shared/chart/chart-engine';
import { TimeSeriesChart } from '../../shared/chart/time-series-chart';
import { DataTable, type TableColumn } from '../../shared/ui/data-table/data-table';
import { PageHeader } from '../../shared/ui/page-header';
import { ModeStamp } from '../../shared/ui/mode-stamp';
import { EmptyState, ErrorState, LoadingState } from '../../shared/ui/states';

const COLUMNS: TableColumn<DemoPositionView>[] = [
  { key: 'ticker', label: 'Ticker', mobile: 'title' },
  { key: 'name', label: 'Name', mobile: 'inline' },
  { key: 'quantity', label: 'Shares', format: 'number' },
  { key: 'price', label: 'Price', format: 'money' },
  { key: 'value', label: 'Value', format: 'money', mobile: 'figure' },
  { key: 'weight', label: 'Weight', format: 'percent' },
  { key: 'pnl', label: 'P&L', format: 'signedMoney', tone: true },
  { key: 'pnl_pct', label: 'Return', format: 'signedPercent', tone: true, mobile: 'figure' },
];

/**
 * The demo portfolio (roadmap 23.17): a sample book with made-up
 * instruments and prices, so a new person can see what Stonks shows before
 * any real data. It never mixes with real books or the lake, and it is
 * labelled "Sample data" everywhere it shows.
 */
@Component({
  selector: 'app-demo-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    RouterLink,
    PageHeader,
    ModeStamp,
    DataTable,
    TimeSeriesChart,
    EmptyState,
    ErrorState,
    LoadingState,
  ],
  template: `
    <app-page-header
      title="Demo portfolio"
      description="A sample paper book to look around in. The instruments and prices are made up."
    />

    @if (demo.error(); as err) {
      <app-error-state title="Could not load the demo" [error]="err" (retry)="demo.reload()" />
    } @else if (!demo.hasValue()) {
      <app-loading-state label="Loading the demo" [rows]="4" />
    } @else if (!view()?.exists) {
      <section class="page-section">
        <app-empty-state
          compact
          title="See Stonks with sample data"
          message="Open a demo portfolio: five made-up holdings and a year of made-up prices. It never touches your real books, and you can remove it at any time."
        >
          <button type="button" class="btn btn-primary" [disabled]="busy()" (click)="open()">
            {{ busy() ? 'Opening…' : 'Open the demo' }}
          </button>
        </app-empty-state>
      </section>
    } @else {
      @let d = view()!;
      <p class="band demo-band" role="note">
        <app-mode-stamp [live]="false" />
        <span class="label">{{ d.label }}</span>
        <span class="why"
          >Nothing here is real. The figures are generated and no order can use them.</span
        >
      </p>

      <section class="overview" aria-labelledby="demo-value">
        <div class="main">
          <h2 id="demo-value" class="ov-title">Value</h2>
          <p class="headline-figure">{{ money(d.total_value) }}</p>
          <p class="change">
            <span class="pill num" [attr.data-tone]="tone(d.day_change)">{{
              money(d.day_change, true)
            }}</span>
            @if (dayPct(); as p) {
              <span class="num" [class]="tone(d.day_change)">{{ p }}</span>
            }
            <span class="when">on the last sample day</span>
          </p>
          <div class="curve">
            <app-time-series-chart
              ariaLabel="Demo portfolio value over the year (sample data)"
              [summary]="summary()"
              [series]="series()"
              [height]="240"
            />
            <p class="hint">
              Marked <span class="num">{{ day(d.as_of) }}</span>
            </p>
          </div>
        </div>
        <aside class="facts" aria-label="Demo facts">
          <dl class="fact-list">
            <div>
              <dt>Return</dt>
              <dd [class]="tone(d.total_return)">{{ pct(d.total_return) }}</dd>
            </div>
            <div>
              <dt>Started with</dt>
              <dd>{{ money(d.start_value) }}</dd>
            </div>
            <div>
              <dt>Cash</dt>
              <dd>{{ money(d.cash) }}</dd>
            </div>
            <div>
              <dt>Holdings</dt>
              <dd>{{ d.positions?.length ?? 0 }}</dd>
            </div>
          </dl>
        </aside>
      </section>

      <section class="page-section section-flush" aria-labelledby="demo-holdings">
        <header class="section-head">
          <h2 id="demo-holdings">
            Holdings <span class="count">{{ d.positions?.length ?? 0 }}</span>
          </h2>
        </header>
        <app-data-table
          caption="Demo holdings (sample data)"
          [rows]="d.positions ?? []"
          [columns]="columns"
          [rowKey]="rowKey"
        />
      </section>

      <section class="band next" aria-label="Your own portfolio">
        <p>Ready for your own? Open a portfolio, or bring a broker's history in.</p>
        <div class="actions">
          <a class="btn btn-primary" routerLink="/welcome">Set up your own</a>
          <a class="btn" routerLink="/connections/import">Import a CSV statement</a>
          <button type="button" class="btn btn-ghost" [disabled]="busy()" (click)="remove()">
            Remove the demo
          </button>
        </div>
      </section>
    }
  `,
  styles: `
    @use 'breakpoints' as bp;

    :host {
      display: grid;
      gap: var(--section-gap);
      grid-template-columns: minmax(0, 1fr);
      min-width: 0;
    }
    app-page-header {
      margin-bottom: -28px;
    }
    /* The demo band: the PAPER stamp, the sample label and why. */
    .demo-band {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: var(--space-2) var(--space-3);
      margin: 0;
    }
    .label {
      font-weight: var(--weight-semibold);
    }
    .why {
      color: var(--ink-2);
      font-size: var(--text-md);
    }
    /* The value and its curve, with a facts column from desktop. */
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
    .ov-title {
      font-size: var(--text-lead);
      font-weight: var(--weight-semibold);
    }
    .headline-figure {
      margin-top: var(--space-1);

      @include bp.phone {
        font-size: clamp(1.75rem, 9.5vw, 2.5rem);
      }
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
    .when {
      color: var(--ink-2);
    }
    .curve {
      margin-top: var(--space-5);
    }
    .hint {
      margin-top: var(--space-2);
      color: var(--muted);
      font-size: var(--text-sm);
    }
    .facts {
      min-width: 0;

      @include bp.from-desktop {
        padding-top: var(--space-6);
      }
    }
    .next {
      display: grid;
      gap: var(--space-3);
    }
    .actions {
      display: flex;
      flex-wrap: wrap;
      gap: var(--space-2);
    }
    @include bp.phone {
      .actions {
        display: grid;
        grid-template-columns: 1fr 1fr;
      }
      .actions .btn-primary {
        grid-column: 1 / -1;
      }
    }
  `,
})
export class DemoPage {
  private readonly api = inject(DemoService);
  private readonly confirm = inject(ConfirmService);

  protected readonly columns = COLUMNS;
  protected readonly rowKey = (p: DemoPositionView) => p.ticker;
  protected readonly busy = signal(false);
  protected readonly demo = resource({ loader: () => this.api.get() });
  /** A fresh open or remove replaces the loaded view without a reload. */
  private readonly override = signal<DemoPortfolioView | null>(null);
  protected readonly view = computed<DemoPortfolioView | null>(
    () => this.override() ?? (this.demo.hasValue() ? this.demo.value() : null),
  );

  protected readonly series = computed<ChartSeries[]>(() => [
    {
      id: 'value',
      label: 'Value (sample)',
      kind: 'line',
      color: 'primary',
      format: 'money',
      points: (this.view()?.curve ?? []).map((p) => ({ time: p.day, value: p.value })),
    },
  ]);

  protected readonly summary = computed(() => {
    const d = this.view();
    if (!d?.exists) return null;
    return `Sample data: from ${formatMoney(d.start_value)} to ${formatMoney(d.total_value)}, ${formatPercent(d.total_return, { signed: true })}.`;
  });

  protected readonly dayPct = computed(() => {
    const d = this.view();
    if (!d?.exists || !d.total_value || d.day_change == null) return null;
    return formatPercent(d.day_change / (d.total_value - d.day_change), { signed: true });
  });

  protected money(value: number | null | undefined, signed = false): string {
    return formatMoney(value, { signed });
  }

  protected pct(value: number | null | undefined): string {
    return formatPercent(value, { signed: true });
  }

  protected tone(value: number | null | undefined): 'gain' | 'loss' | '' {
    return toneClass(value);
  }

  protected day(value: string | null | undefined): string {
    return formatDate(value);
  }

  protected async open(): Promise<void> {
    this.busy.set(true);
    try {
      this.override.set(await this.api.open());
    } catch {
      // The error interceptor already showed the API's message.
    } finally {
      this.busy.set(false);
    }
  }

  protected async remove(): Promise<void> {
    const ok = await this.confirm.confirm({
      title: 'Remove the demo portfolio?',
      message: 'Only the sample goes. Nothing real changes. You can open a new one later.',
      confirmLabel: 'Remove demo',
    });
    if (!ok) return;
    this.busy.set(true);
    try {
      await this.api.remove();
      this.override.set({ exists: false, label: 'Sample data' } as DemoPortfolioView);
    } catch {
      // The error interceptor already showed the API's message.
    } finally {
      this.busy.set(false);
    }
  }
}
