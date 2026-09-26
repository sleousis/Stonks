import {
  ChangeDetectionStrategy,
  Component,
  inject,
  linkedSignal,
  resource,
  signal,
} from '@angular/core';
import { RouterLink } from '@angular/router';

import type { TickRun } from '../../api/models';
import { TicksService } from '../../api/ticks.service';
import { formatDuration } from '../../core/format/format';
import { autoRefresh } from '../../shared/auto-refresh';
import { keepLatest } from '../../shared/ui/data-table/keep-latest';
import { DataTable, TableCell, type TableColumn } from '../../shared/ui/data-table/data-table';
import { EmptyState, ErrorState, LoadingState } from '../../shared/ui/states';
import { DateTimePipe } from '../../shared/format.pipes';
import { StatusPill } from '../../shared/ui/status-pill';
import { TickRunner } from './tick-runner';
import { tickOutcome } from './tick-summary';

const PAGE_SIZE = 25;

const TICK_STATUSES = [
  { value: 'ok', label: 'OK' },
  { value: 'partial', label: 'Partial' },
  { value: 'error', label: 'Error' },
  { value: 'running', label: 'Running' },
];

/** Trading run history (server paged, filter by status) and the runner. */
@Component({
  selector: 'app-ticks-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    RouterLink,
    DateTimePipe,
    DataTable,
    TableCell,
    StatusPill,
    TickRunner,
    LoadingState,
    EmptyState,
    ErrorState,
  ],
  template: `
    <div class="page-grid">
      <section class="panel span-8 history" aria-labelledby="ticks-title">
        <div class="panel-head">
          <h2 id="ticks-title">Trading runs</h2>
          <div class="head-tools">
            <label class="visually-hidden" for="tick-status">Status</label>
            <select
              id="tick-status"
              class="input"
              [value]="status()"
              (change)="status.set($any($event.target).value)"
            >
              <option value="">Any status</option>
              @for (s of statuses; track s.value) {
                <option [value]="s.value" [selected]="s.value === status()">{{ s.label }}</option>
              }
            </select>
          </div>
        </div>
        @let p = page();
        @if (ticks.error(); as err) {
          <app-error-state
            title="Could not load trading runs"
            [error]="err"
            (retry)="ticks.reload()"
          />
        } @else if (!p) {
          <app-loading-state label="Loading trading runs" [rows]="6" />
        } @else if (p.items.length === 0) {
          <app-empty-state
            [title]="status() ? 'No runs with this status' : 'No trading runs yet'"
            [message]="
              status()
                ? 'Pick another status or show all runs.'
                : 'Start a dry run to see what the active strategies would trade.'
            "
          />
        } @else {
          @for (k of [status()]; track k) {
            <app-data-table
              caption="Trading runs, newest first"
              [rows]="p.items"
              [columns]="columns"
              [rowKey]="key"
              [total]="p.total"
              [offset]="p.offset"
              [busy]="ticks.isLoading()"
              [pageSize]="pageSize"
              [initialSort]="{ key: 'started_at', dir: 'desc' }"
              (pageChange)="offset.set($event.offset)"
            >
              <ng-template appCell="started_at" [appCellOf]="p.items" let-t>
                <a class="cell-link" [routerLink]="['/orders/ticks', t.id]">{{
                  t.started_at | dateTime
                }}</a>
              </ng-template>
              <ng-template appCell="status" [appCellOf]="p.items" let-t>
                <app-status-pill [status]="t.status" />
              </ng-template>
              <ng-template appCell="outcome" [appCellOf]="p.items" let-t>
                <span
                  [class.muted]="outcome(t).kind === 'none'"
                  [class.loss]="outcome(t).kind === 'error'"
                >
                  {{ outcome(t).text }}
                </span>
              </ng-template>
            </app-data-table>
          }
        }
      </section>

      <app-tick-runner class="span-4 runner" (finished)="ticks.reload()" />
    </div>
  `,
  styleUrl: './orders-views.scss',
  styles: `
    @use 'breakpoints' as bp;

    .head-tools select {
      min-width: 9rem;
    }
    @include bp.phone {
      .runner {
        order: -1;
      }
    }
    @include bp.from-desktop {
      .runner {
        align-self: start;
      }
    }
  `,
})
export class TicksPage {
  private readonly ticksApi = inject(TicksService);

  protected readonly pageSize = PAGE_SIZE;
  protected readonly statuses = TICK_STATUSES;
  protected readonly status = signal<TickRun['status'] | ''>('');
  protected readonly offset = linkedSignal({ source: this.status, computation: () => 0 });

  protected readonly ticks = resource({
    params: () => ({ status: this.status() || null, limit: PAGE_SIZE, offset: this.offset() }),
    loader: ({ params }) => this.ticksApi.list(params),
  });
  /** The last loaded page stays on screen while the next one loads. */
  protected readonly page = keepLatest(this.ticks);
  protected readonly auto = autoRefresh(() => [this.ticks]);

  protected readonly columns: TableColumn<TickRun>[] = [
    { key: 'started_at', label: 'Started', format: 'datetime', mobile: 'title' },
    { key: 'status', label: 'Status' },
    {
      key: 'orders',
      label: 'Orders',
      format: 'number',
      value: (t) => t.summary?.orders_placed ?? null,
    },
    { key: 'fills', label: 'Fills', format: 'number', value: (t) => t.summary?.fills ?? null },
    { key: 'outcome', label: 'Winner or exit', value: (t) => tickOutcome(t.summary).text },
    {
      key: 'duration',
      label: 'Took',
      sortable: false,
      align: 'end',
      mobile: 'hide',
      value: (t) => formatDuration(t.started_at, t.finished_at),
    },
  ];
  protected readonly key = (t: TickRun) => t.id;
  protected readonly outcome = (t: TickRun) => tickOutcome(t.summary);
}
