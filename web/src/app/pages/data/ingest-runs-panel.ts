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
import { keepLatest } from '../../shared/ui/data-table/keep-latest';
import { StatusPill } from '../../shared/ui/status-pill';
import { kindLabel, runStatusLabel, sourceLabel } from './data-labels';
import { INGEST_KINDS } from './ingest-request';

const PAGE_SIZE = 15;
const STATUSES = ['running', 'ok', 'partial', 'error'] as const;

/** Data update history: what ran, how it ended, counts and the provider's error text. */
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
        <h2 id="runs-title">Data updates</h2>
        @if (page(); as p) {
          <span class="muted num count">{{ p.total }} {{ p.total === 1 ? 'update' : 'updates' }}</span>
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
              <option [value]="s">{{ statusLabel(s) }}</option>
            }
          </select>
        </div>
        <div class="field">
          <label for="runs-kind">What</label>
          <select
            id="runs-kind"
            class="input"
            [value]="kind()"
            (change)="kind.set($any($event.target).value)"
          >
            <option value="">Anything</option>
            @for (k of kinds; track k.value) {
              <option [value]="k.value">{{ k.label }}</option>
            }
          </select>
        </div>
      </div>
      @let p = page();
      @if (list.error(); as err) {
        <app-error-state
          title="Could not load data updates"
          [error]="err"
          (retry)="list.reload()"
        />
      } @else if (!p) {
        <app-loading-state label="Loading data updates" [rows]="5" />
      } @else if (p.items.length === 0) {
        <app-empty-state
          title="No data updates"
          [message]="
            status() || kind()
              ? 'No updates match these filters.'
              : 'Updates appear here after Update data or the daily schedule runs.'
          "
        />
      } @else {
        <app-data-table
          caption="Data updates, newest first"
          [rows]="p.items"
          [columns]="columns"
          [rowKey]="key"
          [total]="p.total"
          [offset]="p.offset"
          [busy]="list.isLoading()"
          [pageSize]="pageSize"
          (pageChange)="offset.set($event.offset)"
        >
          <ng-template appCell="status" [appCellOf]="p.items" let-run>
            <app-status-pill [status]="run.status ?? 'unknown'" [label]="statusLabel(run.status)" />
          </ng-template>
          <ng-template appCell="error" [appCellOf]="p.items" let-run>
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
  /** The last loaded page stays on screen while the next one loads. */
  protected readonly page = keepLatest(this.list);

  protected readonly columns: TableColumn<IngestRunView>[] = [
    { key: 'started_at', label: 'Started', format: 'datetime', mobile: 'title' },
    { key: 'kind', label: 'What', value: (r) => kindLabel(r.kind) },
    { key: 'source', label: 'Source', value: (r) => sourceLabel(r.source), mobile: 'hide' },
    { key: 'status', label: 'Status' },
    { key: 'tickers_ok', label: 'Updated', format: 'number' },
    { key: 'tickers_failed', label: 'Failed', format: 'number' },
    { key: 'error', label: 'Error', sortable: false },
  ];
  protected readonly key = (r: IngestRunView) => String(r.id);
  protected readonly statusLabel = runStatusLabel;

  reload(): void {
    this.list.reload();
  }
}
