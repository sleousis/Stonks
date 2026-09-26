import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  input,
  linkedSignal,
  resource,
} from '@angular/core';
import { ActivatedRoute, Router } from '@angular/router';

import { OrdersService } from '../../api/orders.service';
import { EmptyState, ErrorState, LoadingState } from '../../shared/ui/states';
import { ORDER_STATUS_OPTIONS } from './order-status';
import { OrdersTable } from './orders-table';

const PAGE_SIZE = 50;

/**
 * Orders placed by ticks, filtered from the server. Filters live in the URL
 * (`?ticker=&strategy=&tick=&status=`) so other pages can link to a filtered
 * view and the back button restores it.
 */
@Component({
  selector: 'app-orders-list-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [OrdersTable, LoadingState, EmptyState, ErrorState],
  template: `
    <section class="panel" aria-labelledby="orders-title">
      <div class="panel-head">
        <h2 id="orders-title">Orders</h2>
        @if (orders.hasValue()) {
          <span class="count num">{{ orders.value().total }} matching</span>
        }
      </div>

      <form
        class="filters"
        role="search"
        aria-label="Filter orders"
        (submit)="$event.preventDefault()"
      >
        <div class="field">
          <label for="f-ticker">Ticker</label>
          <input
            id="f-ticker"
            class="input"
            placeholder="AAPL.US"
            autocapitalize="characters"
            [value]="ticker() ?? ''"
            (change)="setFilter('ticker', $any($event.target).value)"
          />
        </div>
        <div class="field">
          <label for="f-strategy">Strategy</label>
          <input
            id="f-strategy"
            class="input"
            placeholder="Strategy id"
            [value]="strategy() ?? ''"
            (change)="setFilter('strategy', $any($event.target).value)"
          />
        </div>
        <div class="field">
          <label for="f-tick">Tick</label>
          <input
            id="f-tick"
            class="input"
            placeholder="Tick id"
            [value]="tick() ?? ''"
            (change)="setFilter('tick', $any($event.target).value)"
          />
        </div>
        <div class="field">
          <label for="f-status">Status</label>
          <select
            id="f-status"
            class="input"
            [value]="status() ?? ''"
            (change)="setFilter('status', $any($event.target).value)"
          >
            <option value="">Any status</option>
            @for (s of statusOptions; track s.value) {
              <option [value]="s.value" [selected]="s.value === status()">{{ s.label }}</option>
            }
          </select>
        </div>
        <div class="filter-actions">
          <button type="button" class="btn" [disabled]="!hasFilters()" (click)="clearFilters()">
            Clear filters
          </button>
        </div>
      </form>

      @if (orders.error(); as err) {
        <app-error-state title="Could not load orders" [error]="err" (retry)="orders.reload()" />
      } @else if (!orders.hasValue()) {
        <app-loading-state label="Loading orders" [rows]="6" />
      } @else if (orders.value().items.length === 0) {
        @if (hasFilters()) {
          <app-empty-state
            title="No orders match these filters"
            message="Clear a filter or widen the search; filters match ids exactly."
          />
        } @else {
          <app-empty-state
            title="No orders yet"
            message="Orders appear here after a tick runs. Start one from the Ticks tab (dry run first)."
          />
        }
      } @else {
        <!-- Re-created per filter set so paging restarts on page one. -->
        @for (k of [filterKey()]; track k) {
          <app-orders-table
            [rows]="orders.value().items"
            [total]="orders.value().total"
            [offset]="orders.value().offset"
            [pageSize]="pageSize"
            (pageChange)="offset.set($event.offset)"
          />
        }
      }
    </section>
  `,
  styleUrl: './orders-views.scss',
})
export class OrdersListPage {
  private readonly ordersApi = inject(OrdersService);
  private readonly router = inject(Router);
  private readonly route = inject(ActivatedRoute);

  // Query params, bound by the router.
  readonly ticker = input<string>();
  readonly strategy = input<string>();
  readonly tick = input<string>();
  readonly status = input<string>();

  protected readonly pageSize = PAGE_SIZE;
  protected readonly statusOptions = ORDER_STATUS_OPTIONS;

  private readonly filters = computed(() => ({
    ticker: this.ticker() || null,
    strategy_id: this.strategy() || null,
    tick_id: this.tick() || null,
    status: this.status() || null,
  }));
  protected readonly filterKey = computed(() => JSON.stringify(this.filters()));
  protected readonly hasFilters = computed(() => Object.values(this.filters()).some(Boolean));
  protected readonly offset = linkedSignal({ source: this.filterKey, computation: () => 0 });

  protected readonly orders = resource({
    params: () => ({ ...this.filters(), limit: PAGE_SIZE, offset: this.offset() }),
    loader: ({ params }) => this.ordersApi.list(params),
  });

  protected setFilter(name: 'ticker' | 'strategy' | 'tick' | 'status', raw: string): void {
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
