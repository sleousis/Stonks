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
import { autoRefresh } from '../../shared/auto-refresh';
import { keepLatest } from '../../shared/ui/data-table/keep-latest';
import { EmptyState, ErrorState, LoadingState } from '../../shared/ui/states';
import { ORDER_STATUS_OPTIONS } from './order-status';
import { OrdersTable } from './orders-table';
import { PortfolioContextService } from '../../core/portfolio/portfolio-context.service';

const PAGE_SIZE = 50;

/**
 * Orders placed by trading runs, filtered from the server. Filters live in the URL
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
        @if (page(); as p) {
          <span class="count num">{{ p.total }} matching</span>
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
          <label for="f-tick">Trading run</label>
          <input
            id="f-tick"
            class="input"
            placeholder="Run id"
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

      @let p = page();
      @if (orders.error(); as err) {
        <app-error-state title="Could not load orders" [error]="err" (retry)="orders.reload()" />
      } @else if (!p) {
        <app-loading-state label="Loading orders" [rows]="6" />
      } @else if (p.items.length === 0) {
        @if (hasFilters()) {
          <app-empty-state
            title="No orders match these filters"
            message="Clear a filter or widen the search. Filters match ids exactly."
          />
        } @else {
          <app-empty-state
            title="No orders yet"
            message="Orders appear here after a trading run. Start one from the Trading runs tab, with a dry run first."
          />
        }
      } @else {
        <!-- Re-created per filter set so paging restarts on page one. -->
        @for (k of [filterKey()]; track k) {
          <app-orders-table
            [rows]="p.items"
            [total]="p.total"
            [offset]="p.offset"
            [busy]="orders.isLoading()"
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
  private readonly portfolioCtx = inject(PortfolioContextService);
  protected readonly filterKey = computed(() =>
    JSON.stringify({ ...this.filters(), portfolio: this.portfolioCtx.selectedId() }),
  );
  protected readonly hasFilters = computed(() => Object.values(this.filters()).some(Boolean));
  protected readonly offset = linkedSignal({ source: this.filterKey, computation: () => 0 });

  protected readonly orders = resource({
    params: () => ({
      ...this.filters(),
      ...this.portfolioCtx.query(),
      limit: PAGE_SIZE,
      offset: this.offset(),
    }),
    loader: ({ params }) => this.ordersApi.list(params),
  });
  /** The last loaded page stays on screen while the next one loads. */
  protected readonly page = keepLatest(this.orders);
  protected readonly auto = autoRefresh(() => [this.orders]);

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
