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
import { autoRefresh } from '../../shared/auto-refresh';
import { keepLatest } from '../../shared/ui/data-table/keep-latest';
import { DataTable, TableCell, type TableColumn } from '../../shared/ui/data-table/data-table';
import { EmptyState, ErrorState, LoadingState } from '../../shared/ui/states';
import { SideTag } from '../../shared/ui/side-tag';
import { PortfolioContextService } from '../../core/portfolio/portfolio-context.service';

const PAGE_SIZE = 50;

/** Columns shared with the tick drill-down. */
export const FILL_COLUMNS: TableColumn<FillView>[] = [
  { key: 'ticker', label: 'Ticker', mobile: 'title' },
  { key: 'side', label: 'Side' },
  { key: 'quantity', label: 'Qty', format: 'number' },
  { key: 'price', label: 'Price', format: 'money' },
  { key: 'value', label: 'Value', format: 'money', value: (f) => f.quantity * f.price },
  { key: 'fee', label: 'Fee', format: 'money' },
  { key: 'tick_id', label: 'Run', sortable: false, mobile: 'hide' },
  { key: 'order_client_id', label: 'Order', sortable: false, mobile: 'hide' },
  { key: 'filled_at', label: 'Filled', format: 'datetime' },
];

/** Fills received for orders, filtered from the server; filters live in the URL. */
@Component({
  selector: 'app-fills-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink, DataTable, TableCell, LoadingState, EmptyState, ErrorState, SideTag],
  template: `
    <section class="panel" aria-labelledby="fills-title">
      <div class="panel-head">
        <h2 id="fills-title">Fills</h2>
        @if (page(); as p) {
          <span class="count num">{{ p.total }} matching</span>
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
          <label for="ff-tick">Trading run</label>
          <input
            id="ff-tick"
            class="input"
            placeholder="Run id"
            [value]="tick() ?? ''"
            (change)="setFilter('tick', $any($event.target).value)"
          />
        </div>
        <div class="field">
          <label for="ff-order">Order</label>
          <input
            id="ff-order"
            class="input"
            placeholder="Order id"
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

      @let p = page();
      @if (fills.error(); as err) {
        <app-error-state title="Could not load fills" [error]="err" (retry)="fills.reload()" />
      } @else if (!p) {
        <app-loading-state label="Loading fills" [rows]="6" />
      } @else if (p.items.length === 0) {
        <app-empty-state
          [title]="hasFilters() ? 'No fills match these filters' : 'No fills yet'"
          [message]="
            hasFilters()
              ? 'Clear a filter. Ids must match exactly.'
              : 'Fills are recorded when a real trading run (not a dry run) sends orders the broker fills.'
          "
        >
          @if (hasFilters()) {
            <button type="button" class="btn" (click)="clearFilters()">Clear filters</button>
          } @else {
            <a class="btn" routerLink="/orders/ticks">Go to trading runs</a>
          }
        </app-empty-state>
      } @else {
        @for (k of [filterKey()]; track k) {
          <app-data-table
            caption="Fills received for orders"
            [rows]="p.items"
            [columns]="columns"
            [rowKey]="key"
            [total]="p.total"
            [offset]="p.offset"
            [busy]="fills.isLoading()"
            [pageSize]="pageSize"
            [initialSort]="{ key: 'filled_at', dir: 'desc' }"
            (pageChange)="offset.set($event.offset)"
          >
            <ng-template appCell="side" [appCellOf]="p.items" let-f>
              <app-side-tag [side]="f.side" />
            </ng-template>
            <ng-template appCell="tick_id" [appCellOf]="p.items" let-f>
              @if (f.tick_id) {
                <a class="cell-link" [routerLink]="['/orders/ticks', f.tick_id]">View run</a>
              } @else {
                <span class="muted">–</span>
              }
            </ng-template>
            <ng-template appCell="order_client_id" [appCellOf]="p.items" let-f>
              <a class="cell-link" [routerLink]="['/trades/orders', f.order_client_id]"
                >View order</a
              >
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
  private readonly portfolioCtx = inject(PortfolioContextService);
  protected readonly filterKey = computed(() =>
    JSON.stringify({ ...this.filters(), portfolio: this.portfolioCtx.selectedId() }),
  );
  protected readonly hasFilters = computed(() => Object.values(this.filters()).some(Boolean));
  protected readonly offset = linkedSignal({ source: this.filterKey, computation: () => 0 });

  protected readonly fills = resource({
    params: () => ({
      ...this.filters(),
      ...this.portfolioCtx.query(),
      limit: PAGE_SIZE,
      offset: this.offset(),
    }),
    loader: ({ params }) => this.ordersApi.fills(params),
  });
  /** The last loaded page stays on screen while the next one loads. */
  protected readonly page = keepLatest(this.fills);
  protected readonly auto = autoRefresh(() => [this.fills]);

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
