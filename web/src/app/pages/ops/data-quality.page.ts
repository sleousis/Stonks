import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  resource,
  signal,
} from '@angular/core';
import { RouterLink } from '@angular/router';

import type { StatementFlagView } from '../../api/models';
import { OperationsService } from '../../api/operations.service';
import { DataTable, TableCell, type TableColumn } from '../../shared/ui/data-table/data-table';
import { PageHeader } from '../../shared/ui/page-header';
import { humanize } from '../../shared/ui/param-form/param-spec';
import { EmptyState, ErrorState, LoadingState } from '../../shared/ui/states';
import { StatusPill } from '../../shared/ui/status-pill';

export const FLAGS_PAGE_SIZE = 50;
type SeverityFilter = '' | StatementFlagView['severity'];

/**
 * Statement audit flags: periods whose income statement, balance sheet or
 * cash flow failed a check (totals that do not add up, impossible values).
 * The lab preflight warns when a run's universe has `error` flags.
 */
@Component({
  selector: 'app-data-quality-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    PageHeader,
    DataTable,
    TableCell,
    StatusPill,
    LoadingState,
    EmptyState,
    ErrorState,
    RouterLink,
  ],
  template: `
    <app-page-header
      title="Data quality"
      description="Company report periods that failed a check. Lab runs warn when their tickers have errors here."
    />

    <section class="panel" aria-labelledby="flags-title">
      <div class="panel-head">
        <h2 id="flags-title">Statement flags</h2>
        @if (flags.hasValue()) {
          <span class="muted">{{ flags.value().total }} flagged</span>
        }
      </div>
      <form class="filters panel-body" (submit)="$event.preventDefault(); apply(tickerInput.value)">
        <div class="field">
          <label for="flag-ticker">Ticker</label>
          <input
            #tickerInput
            id="flag-ticker"
            class="input"
            placeholder="e.g. AAPL.US"
            autocapitalize="characters"
            spellcheck="false"
            [value]="ticker()"
          />
        </div>
        <div class="field">
          <label for="flag-severity">Severity</label>
          <select
            id="flag-severity"
            class="input"
            [value]="severity()"
            (change)="setSeverity($any($event.target).value)"
          >
            <option value="">All</option>
            <option value="error">Errors</option>
            <option value="warning">Warnings</option>
          </select>
        </div>
        <div class="actions">
          <button type="submit" class="btn">Filter</button>
          @if (filtered()) {
            <button type="button" class="btn btn-ghost" (click)="reset(tickerInput)">
              Show all
            </button>
          }
        </div>
      </form>

      @if (flags.error(); as err) {
        <app-error-state title="Could not load flags" [error]="err" (retry)="flags.reload()" />
      } @else if (!flags.hasValue()) {
        <app-loading-state label="Loading statement flags" [rows]="5" />
      } @else if (flags.value().items.length === 0) {
        <app-empty-state
          [title]="filtered() ? 'No flags match' : 'No statement flags'"
          [message]="
            filtered()
              ? 'Try another ticker or severity.'
              : 'Company reports are checked each time their figures are updated. Periods that do not add up show here.'
          "
        />
      } @else {
        @let page = flags.value();
        <app-data-table
          caption="Flagged statement periods"
          [rows]="page.items"
          [columns]="columns"
          [rowKey]="flagKey"
          [total]="page.total"
          [offset]="page.offset"
          [pageSize]="pageSize"
          (pageChange)="offset.set($event.offset)"
        >
          <ng-template appCell="ticker" [appCellOf]="page.items" let-f>
            <a class="num cell-link" routerLink="/data" [queryParams]="{ instrument: f.ticker }">{{
              f.ticker
            }}</a>
          </ng-template>
          <ng-template appCell="severity" [appCellOf]="page.items" let-f>
            <app-status-pill
              [status]="f.severity"
              [label]="f.severity === 'error' ? 'Error' : 'Warning'"
            />
          </ng-template>
        </app-data-table>
      }
    </section>
  `,
  styles: `
    @use 'breakpoints' as bp;
    .filters {
      display: grid;
      gap: var(--space-3);
      align-items: end;
      border-bottom: 1px solid var(--color-border);
      @include bp.from-tablet {
        grid-template-columns: minmax(0, 14rem) minmax(0, 10rem) auto;
      }
    }
    @include bp.phone {
      .cell-link {
        display: inline-flex;
        align-items: center;
        min-height: var(--touch-min);
      }
    }
    .actions {
      display: flex;
      flex-wrap: wrap;
      gap: var(--space-2);
    }
  `,
})
export class DataQualityPage {
  private readonly ops = inject(OperationsService);

  protected readonly pageSize = FLAGS_PAGE_SIZE;
  protected readonly ticker = signal('');
  protected readonly severity = signal<SeverityFilter>('');
  protected readonly offset = signal(0);
  protected readonly filtered = computed(() => !!this.ticker() || !!this.severity());

  protected readonly flags = resource({
    params: () => ({
      ticker: this.ticker() || null,
      severity: this.severity() || null,
      limit: FLAGS_PAGE_SIZE,
      offset: this.offset(),
    }),
    loader: ({ params }) => this.ops.statementFlags(params),
  });

  protected readonly flagKey = (f: StatementFlagView) =>
    `${f.ticker}|${f.period_end}|${f.frequency}|${f.check_id}`;

  protected readonly columns: TableColumn<StatementFlagView>[] = [
    { key: 'ticker', label: 'Ticker', mobile: 'title' },
    { key: 'period_end', label: 'Period end', format: 'date' },
    { key: 'frequency', label: 'Frequency', value: (f) => humanize(f.frequency) },
    { key: 'check_id', label: 'Check', value: (f) => humanize(f.check_id) },
    { key: 'severity', label: 'Severity' },
    { key: 'detail', label: 'Detail', sortable: false },
    { key: 'flagged_at', label: 'Flagged', format: 'datetime', mobile: 'hide' },
  ];

  protected apply(raw: string): void {
    this.ticker.set(raw.trim().toUpperCase());
    this.offset.set(0);
  }

  protected setSeverity(value: SeverityFilter): void {
    this.severity.set(value);
    this.offset.set(0);
  }

  protected reset(input: HTMLInputElement): void {
    input.value = '';
    this.ticker.set('');
    this.severity.set('');
    this.offset.set(0);
  }
}
