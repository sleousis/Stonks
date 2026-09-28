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
import { StatTile } from '../../shared/ui/stat-tile';
import { EmptyState, ErrorState, LoadingState } from '../../shared/ui/states';

const COLUMNS: TableColumn<DemoPositionView>[] = [
  { key: 'ticker', label: 'Ticker', mobile: 'title' },
  { key: 'name', label: 'Name' },
  { key: 'quantity', label: 'Shares', format: 'number' },
  { key: 'price', label: 'Price', format: 'money' },
  { key: 'value', label: 'Value', format: 'money' },
  { key: 'weight', label: 'Weight', format: 'percent' },
  { key: 'pnl', label: 'P&L', format: 'signedMoney', tone: true },
  { key: 'pnl_pct', label: 'Return', format: 'signedPercent', tone: true },
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
    StatTile,
    DataTable,
    TimeSeriesChart,
    EmptyState,
    ErrorState,
    LoadingState,
  ],
  template: `
    <app-page-header
      title="Demo portfolio"
      description="A sample book to look around in. The instruments and prices are made up."
    />

    @if (demo.error(); as err) {
      <app-error-state title="Could not load the demo" [error]="err" (retry)="demo.reload()" />
    } @else if (!demo.hasValue()) {
      <app-loading-state label="Loading the demo" [rows]="4" />
    } @else if (!view()?.exists) {
      <section class="panel">
        <app-empty-state
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
      <p class="sample" role="note">
        <span class="badge">{{ d.label }}</span>
        Nothing here is real. These figures are generated, and no order can use them.
      </p>

      <div class="tiles">
        <app-stat-tile label="Value" featured [value]="money(d.total_value)" />
        <app-stat-tile
          label="Return"
          [value]="pct(d.total_return)"
          [detail]="'Started with ' + money(d.start_value)"
        />
        <app-stat-tile
          label="Day change"
          [value]="money(d.day_change, true)"
          [detail]="dayPct()"
          [detailTone]="tone(d.day_change)"
        />
        <app-stat-tile label="Cash" [value]="money(d.cash)" />
      </div>

      <section class="panel" aria-labelledby="demo-curve">
        <div class="panel-head">
          <h2 id="demo-curve">Value over the year</h2>
          <span class="hint">Marked {{ day(d.as_of) }}</span>
        </div>
        <div class="panel-body">
          <app-time-series-chart
            ariaLabel="Demo portfolio value over the year (sample data)"
            [summary]="summary()"
            [series]="series()"
            [height]="260"
          />
        </div>
      </section>

      <section class="panel" aria-labelledby="demo-holdings">
        <div class="panel-head">
          <h2 id="demo-holdings">Holdings</h2>
        </div>
        <app-data-table
          caption="Demo holdings (sample data)"
          [rows]="d.positions ?? []"
          [columns]="columns"
          [rowKey]="rowKey"
        />
      </section>

      <section class="panel next">
        <div class="panel-body next-body">
          <p>Ready for your own? Open a portfolio, or bring a broker's history in.</p>
          <div class="actions">
            <a class="btn btn-primary" routerLink="/welcome">Set up your own</a>
            <a class="btn" routerLink="/connections/import">Import a CSV statement</a>
            <button type="button" class="btn btn-ghost" [disabled]="busy()" (click)="remove()">
              Remove the demo
            </button>
          </div>
        </div>
      </section>
    }
  `,
  styles: `
    :host {
      display: grid;
      gap: var(--space-4);
      min-width: 0;
    }
    .sample {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: var(--space-2);
      margin: 0;
      padding: var(--space-3) var(--space-4);
      border: 1px dashed var(--color-border-strong);
      border-radius: var(--radius-md);
      background: var(--color-surface-2);
    }
    .badge {
      padding: 2px var(--space-2);
      border-radius: var(--radius-sm);
      background: var(--color-ink);
      color: var(--color-surface);
      font-size: var(--text-xs);
      font-weight: var(--weight-semibold);
      text-transform: uppercase;
      letter-spacing: 0.04em;
    }
    .tiles {
      display: grid;
      gap: var(--space-3);
      grid-template-columns: repeat(auto-fit, minmax(10rem, 1fr));
    }
    .hint {
      font-size: var(--text-sm);
      color: var(--color-ink-2);
    }
    .next-body {
      display: grid;
      gap: var(--space-3);
    }
    .next-body p {
      margin: 0;
    }
    .actions {
      display: flex;
      flex-wrap: wrap;
      gap: var(--space-2);
    }
    .actions .btn {
      min-height: 44px;
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
