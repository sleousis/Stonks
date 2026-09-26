import {
  ChangeDetectionStrategy,
  Component,
  inject,
  input,
  linkedSignal,
  resource,
  signal,
} from '@angular/core';

import { IngestService } from '../../api/ingest.service';
import type { IngestRunView } from '../../api/models';
import { DataTable, TableCell, type TableColumn } from '../../shared/ui/data-table/data-table';
import { EmptyState, ErrorState, LoadingState } from '../../shared/ui/states';
import { StatusPill } from '../../shared/ui/status-pill';
import { INGEST_KINDS } from './ingest-request';

const PAGE_SIZE = 15;
const STATUSES = ['running', 'ok', 'partial', 'error'] as const;

/** Ingest run history: status, counts and the vendor's error text. */
@Component({
  selector: 'app-ingest-runs-panel',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [DataTable, TableCell, StatusPill, LoadingState, EmptyState, ErrorState],
  styleUrls: ['./data-shared.scss'],
  styles: `
    .error-text {
      display: inline-block;
      min-width: 18ch;
      max-width: 40ch;
      white-space: normal;
      overflow-wrap: anywhere;
      color: var(--color-loss);
      font-size: var(--text-sm);
    }
  `,
  template: `
    <section class="panel" aria-labelledby="runs-title">
      <div class="panel-head">
        <h2 id="runs-title">Ingest history</h2>
        @if (list.hasValue()) {
          <span class="muted num count">{{ list.value().total }} runs</span>
        }
      </div>
      <div class="filters">
        <div class="field">
          <label for="runs-status">Status</label>
          <select
            id="runs-status"
            class="input"
            [value]="status()"
            (change)="status.set($any($event.target).value)"
          >
            <option value="">Any status</option>
            @for (s of statuses; track s) {
              <option [value]="s">{{ s }}</option>
            }
          </select>
        </div>
        <div class="field">
          <label for="runs-kind">Kind</label>
          <select
            id="runs-kind"
            class="input"
            [value]="kind()"
            (change)="kind.set($any($event.target).value)"
          >
            <option value="">Any kind</option>
            @for (k of kinds; track k.value) {
              <option [value]="k.value">{{ k.label }}</option>
            }
          </select>
        </div>
      </div>
      @if (list.error(); as err) {
        <app-error-state title="Could not load ingest runs" [error]="err" (retry)="list.reload()" />
      } @else if (!list.hasValue()) {
        <app-loading-state label="Loading ingest runs" [rows]="5" />
      } @else if (list.value().items.length === 0) {
        <app-empty-state
          title="No ingest runs"
          [message]="
            status() || kind()
              ? 'No runs match these filters.'
              : 'Runs appear here after Run ingest or stonks ingest on the command line.'
          "
        />
      } @else {
        <app-data-table
          caption="Ingest runs, newest first"
          [rows]="list.value().items"
          [columns]="columns"
          [rowKey]="key"
          [total]="list.value().total"
          [pageSize]="pageSize"
          (pageChange)="offset.set($event.offset)"
        >
          <ng-template appCell="status" [appCellOf]="list.value().items" let-run>
            <app-status-pill [status]="run.status ?? 'unknown'" />
          </ng-template>
          <ng-template appCell="error" [appCellOf]="list.value().items" let-run>
            @if (run.error) {
              <span class="error-text">{{ run.error }}</span>
            } @else {
              <span class="muted">–</span>
            }
          </ng-template>
        </app-data-table>
      }
    </section>
  `,
})
export class IngestRunsPanel {
  private readonly ingestApi = inject(IngestService);

  /** Bump to refetch (after an ingest). */
  readonly refresh = input(0);

  protected readonly pageSize = PAGE_SIZE;
  protected readonly statuses = STATUSES;
  protected readonly kinds = INGEST_KINDS;
  protected readonly status = signal('');
  protected readonly kind = signal('');
  protected readonly offset = linkedSignal({
    source: () => [this.status(), this.kind()],
    computation: () => 0,
  });

  protected readonly list = resource({
    params: () => {
      this.refresh();
      return {
        status: this.status() || undefined,
        kind: this.kind() || undefined,
        limit: PAGE_SIZE,
        offset: this.offset(),
      };
    },
    loader: ({ params }) => this.ingestApi.runs(params),
  });

  protected readonly columns: TableColumn<IngestRunView>[] = [
    { key: 'id', label: 'Run', mobile: 'title', value: (r) => `#${r.id}`, sortable: false },
    { key: 'started_at', label: 'Started', format: 'datetime' },
    { key: 'source', label: 'Source', mobile: 'hide' },
    { key: 'kind', label: 'Kind' },
    { key: 'status', label: 'Status' },
    { key: 'tickers_ok', label: 'Ok', format: 'number' },
    { key: 'tickers_failed', label: 'Failed', format: 'number' },
    { key: 'error', label: 'Error', sortable: false },
  ];
  protected readonly key = (r: IngestRunView) => String(r.id);

  reload(): void {
    this.list.reload();
  }
}
