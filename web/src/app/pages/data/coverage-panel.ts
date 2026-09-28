import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  input,
  linkedSignal,
  output,
  resource,
} from '@angular/core';

import type { CoverageRow } from '../../api/models';
import { MarketService } from '../../api/market.service';
import { formatDateTime } from '../../core/format/format';
import { DataTable, TableCell, type TableColumn } from '../../shared/ui/data-table/data-table';
import { keepLatest } from '../../shared/ui/data-table/keep-latest';
import { EmptyState, ErrorState, LoadingState } from '../../shared/ui/states';
import { StatusPill } from '../../shared/ui/status-pill';
import { FRESHNESS_LABEL, FRESHNESS_TONE, type Freshness, freshnessOf } from './freshness';

const PAGE_SIZE = 25;
const INTRADAY = /^\d+(m|h)$/;
const RANK: Record<Freshness, number> = { fresh: 0, stale: 1, old: 2 };

export interface CoveragePick {
  ticker: string;
  interval: string;
}

interface CoverageView extends CoverageRow {
  freshness: Freshness;
}

/** "2026-09-25" for daily/weekly bars, a local date and time for intraday. */
export function barTime(timestamp: string, interval: string): string {
  return INTRADAY.test(interval) ? formatDateTime(timestamp) : timestamp.slice(0, 10);
}

/** First/last bar and row count per interval, with a freshness pill. */
@Component({
  selector: 'app-coverage-panel',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [DataTable, TableCell, StatusPill, LoadingState, EmptyState, ErrorState],
  styleUrls: ['./data-shared.scss'],
  template: `
    <section class="panel" aria-labelledby="coverage-title">
      <div class="panel-head">
        <h2 id="coverage-title">
          Coverage
          @if (ticker(); as t) {
            <span class="muted">· {{ t }}</span>
          }
        </h2>
        @if (ticker()) {
          <button type="button" class="btn btn-ghost" (click)="cleared.emit()">
            Show all tickers
          </button>
        } @else if (page(); as p) {
          <span class="muted num count">{{ p.total }} series</span>
        }
      </div>
      @if (list.error(); as err) {
        <app-error-state title="Could not load coverage" [error]="err" (retry)="list.reload()" />
      } @else if (!page()) {
        <app-loading-state label="Loading coverage" [rows]="5" />
      } @else if (rows().length === 0) {
        <app-empty-state
          [title]="ticker() ? 'No price data for ' + ticker() : 'No price data yet'"
          message="Use Update data below to fetch daily or intraday prices."
        />
      } @else {
        <app-data-table
          [caption]="
            ticker()
              ? 'Bars stored for ' + ticker() + ' per interval'
              : 'Bars stored per ticker and interval'
          "
          [rows]="rows()"
          [columns]="columns"
          [rowKey]="key"
          [total]="ticker() ? null : (page()?.total ?? null)"
          [offset]="ticker() ? null : (page()?.offset ?? null)"
          [busy]="list.isLoading()"
          [pageSize]="ticker() ? 0 : pageSize"
          [initialSort]="ticker() ? { key: 'interval', dir: 'asc' } : null"
          (pageChange)="offset.set($event.offset)"
        >
          <ng-template appCell="ticker" [appCellOf]="rows()" let-row>
            <button
              type="button"
              class="pick"
              [attr.aria-label]="'Chart ' + row.ticker + ' ' + row.interval"
              (click)="picked.emit({ ticker: row.ticker, interval: row.interval })"
            >
              {{ row.ticker }}
            </button>
          </ng-template>
          <ng-template appCell="freshness" [appCellOf]="rows()" let-row>
            <app-status-pill
              [status]="row.freshness"
              [tone]="tone[row.freshness]"
              [label]="label[row.freshness]"
            />
          </ng-template>
        </app-data-table>
      }
    </section>
  `,
})
export class CoveragePanel {
  private readonly market = inject(MarketService);

  readonly ticker = input<string | null>(null);
  /** Bump to refetch (after a data update). */
  readonly refresh = input(0);
  readonly picked = output<CoveragePick>();
  readonly cleared = output<void>();

  protected readonly pageSize = PAGE_SIZE;
  protected readonly tone = FRESHNESS_TONE;
  protected readonly label = FRESHNESS_LABEL;
  /** Back to the first page whenever the ticker changes. */
  protected readonly offset = linkedSignal({ source: this.ticker, computation: () => 0 });

  protected readonly list = resource({
    params: () => {
      const ticker = this.ticker();
      this.refresh();
      return ticker
        ? { ticker, limit: 100, offset: 0 }
        : { limit: PAGE_SIZE, offset: this.offset() };
    },
    loader: ({ params }) => this.market.coverage(params),
  });
  /** The last loaded page stays on screen while the next one loads. */
  protected readonly page = keepLatest(this.list);

  protected readonly rows = computed<CoverageView[]>(() => {
    const page = this.page();
    if (!page) return [];
    const now = new Date();
    return page.items.map((r) => ({ ...r, freshness: freshnessOf(r.last_bar, r.interval, now) }));
  });

  protected readonly columns: TableColumn<CoverageView>[] = [
    { key: 'ticker', label: 'Ticker', mobile: 'title' },
    { key: 'interval', label: 'Interval' },
    {
      key: 'first_bar',
      label: 'First price',
      value: (r) => barTime(r.first_bar, r.interval),
      mobile: 'hide',
    },
    { key: 'last_bar', label: 'Last price', value: (r) => barTime(r.last_bar, r.interval) },
    { key: 'rows', label: 'Prices', format: 'number' },
    { key: 'freshness', label: 'Freshness', value: (r) => RANK[r.freshness] },
  ];
  protected readonly key = (r: CoverageView) => `${r.ticker}|${r.interval}`;
}
