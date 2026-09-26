import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  input,
  linkedSignal,
  resource,
} from '@angular/core';
import { ActivatedRoute, Router, RouterLink } from '@angular/router';

import type { FillView } from '../../api/models';
import { OrdersService } from '../../api/orders.service';
import { DataTable, TableCell, type TableColumn } from '../../shared/ui/data-table/data-table';
import { EmptyState, ErrorState, LoadingState } from '../../shared/ui/states';

const PAGE_SIZE = 50;

/** Columns shared with the tick drill-down. */
export const FILL_COLUMNS: TableColumn<FillView>[] = [
  { key: 'ticker', label: 'Ticker', mobile: 'title' },
  { key: 'quantity', label: 'Qty', format: 'number' },
  { key: 'price', label: 'Price', format: 'money' },
  { key: 'value', label: 'Value', format: 'money', value: (f) => f.quantity * f.price },
  { key: 'fee', label: 'Fee', format: 'money' },
  { key: 'tick_id', label: 'Tick', mobile: 'hide' },
  { key: 'order_client_id', label: 'Order', mobile: 'hide' },
  { key: 'filled_at', label: 'Filled', format: 'datetime' },
];

/** Fills received for orders, filtered from the server; filters live in the URL. */
@Component({
  selector: 'app-fills-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink, DataTable, TableCell, LoadingState, EmptyState, ErrorState],
  template: `
    <section class="panel" aria-labelledby="fills-title">
      <div class="panel-head">
        <h2 id="fills-title">Fills</h2>
        @if (fills.hasValue()) {
          <span class="count num">{{ fills.value().total }} matching</span>
        }
      </div>

      <form
        class="filters"
        role="search"
        aria-label="Filter fills"
        (submit)="$event.preventDefault()"
      >
        <div class="field">
          <label for="ff-ticker">Ticker</label>
          <input
            id="ff-ticker"
            class="input"
            placeholder="AAPL.US"
            autocapitalize="characters"
            [value]="ticker() ?? ''"
            (change)="setFilter('ticker', $any($event.target).value)"
          />
        </div>
        <div class="field">
          <label for="ff-tick">Tick</label>
          <input
            id="ff-tick"
            class="input"
            placeholder="Tick id"
            [value]="tick() ?? ''"
            (change)="setFilter('tick', $any($event.target).value)"
          />
        </div>
        <div class="field">
          <label for="ff-order">Order client id</label>
          <input
            id="ff-order"
            class="input"
            placeholder="Client id"
            [value]="order() ?? ''"
            (change)="setFilter('order', $any($event.target).value)"
          />
        </div>
        <div class="filter-actions">
          <button type="button" class="btn" [disabled]="!hasFilters()" (click)="clearFilters()">
            Clear filters
          </button>
        </div>
      </form>

      @if (fills.error(); as err) {
        <app-error-state title="Could not load fills" [error]="err" (retry)="fills.reload()" />
      } @else if (!fills.hasValue()) {
        <app-loading-state label="Loading fills" [rows]="6" />
      } @else if (fills.value().items.length === 0) {
        <app-empty-state
          [title]="hasFilters() ? 'No fills match these filters' : 'No fills yet'"
          [message]="
            hasFilters()
              ? 'Clear a filter; ids must match exactly.'
              : 'Fills are recorded when a real (not dry-run) tick sends orders the broker fills.'
          "
        />
      } @else {
        @for (k of [filterKey()]; track k) {
          <app-data-table
            caption="Fills received for orders"
            [rows]="fills.value().items"
            [columns]="columns"
            [rowKey]="key"
            [total]="fills.value().total"
            [offset]="fills.value().offset"
            [pageSize]="pageSize"
            [initialSort]="{ key: 'filled_at', dir: 'desc' }"
            (pageChange)="offset.set($event.offset)"
          >
            <ng-template appCell="tick_id" [appCellOf]="fills.value().items" let-f>
              @if (f.tick_id) {
                <a class="cell-link mono" [routerLink]="['/orders/ticks', f.tick_id]">{{
                  f.tick_id
                }}</a>
              } @else {
                <span class="mono">–</span>
              }
            </ng-template>
            <ng-template appCell="order_client_id" [appCellOf]="fills.value().items" let-f>
              <span class="mono id">{{ f.order_client_id }}</span>
            </ng-template>
          </app-data-table>
        }
      }
    </section>
  `,
  styleUrl: './orders-views.scss',
})
export class FillsPage {
  private readonly ordersApi = inject(OrdersService);
  private readonly router = inject(Router);
  private readonly route = inject(ActivatedRoute);

  readonly ticker = input<string>();
  readonly tick = input<string>();
  readonly order = input<string>();

  protected readonly pageSize = PAGE_SIZE;
  protected readonly columns = FILL_COLUMNS;
  protected readonly key = (f: FillView) => String(f.id);

  private readonly filters = computed(() => ({
    ticker: this.ticker() || null,
    tick_id: this.tick() || null,
    order_client_id: this.order() || null,
  }));
  protected readonly filterKey = computed(() => JSON.stringify(this.filters()));
  protected readonly hasFilters = computed(() => Object.values(this.filters()).some(Boolean));
  protected readonly offset = linkedSignal({ source: this.filterKey, computation: () => 0 });

  protected readonly fills = resource({
    params: () => ({ ...this.filters(), limit: PAGE_SIZE, offset: this.offset() }),
    loader: ({ params }) => this.ordersApi.fills(params),
  });

  protected setFilter(name: 'ticker' | 'tick' | 'order', raw: string): void {
    let value: string | null = raw.trim() || null;
    if (value && name === 'ticker') value = value.toUpperCase();
    void this.router.navigate([], {
      relativeTo: this.route,
      queryParams: { [name]: value },
      queryParamsHandling: 'merge',
      replaceUrl: true,
    });
  }

  protected clearFilters(): void {
    void this.router.navigate([], { relativeTo: this.route, queryParams: {}, replaceUrl: true });
  }
}
