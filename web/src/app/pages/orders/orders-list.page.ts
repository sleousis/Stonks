import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  input,
  linkedSignal,
  resource,
  signal,
} from '@angular/core';
import { ActivatedRoute, Router, RouterLink } from '@angular/router';

import { OrdersService } from '../../api/orders.service';
import { SubscriptionsService } from '../../api/subscriptions.service';
import { strategyDisplayName } from '../../shared/strategy-names';
import { autoRefresh } from '../../shared/auto-refresh';
import { keepLatest } from '../../shared/ui/data-table/keep-latest';
import { EmptyState, ErrorState, LoadingState } from '../../shared/ui/states';
import { ORDER_STATUS_OPTIONS } from './order-status';
import { OrdersTable } from './orders-table';
import { PortfolioContextService } from '../../core/portfolio/portfolio-context.service';

const PAGE_SIZE = 50;

/**
 * Orders from trading runs and by hand, filtered from the server. Filters
 * live in the URL (`?ticker=&strategy=&tick=&status=`) so other pages can
 * link to a filtered view and the back button restores it. Strategy picks
 * from the followed strategies by name. A run (`?tick=`, from a run's page)
 * shows as a chip to clear, never a box to type an id in (PO-09). Status is a
 * row of filter pills. On phones the ticker and strategy fold behind a
 * Filter button.
 */
@Component({
  selector: 'app-orders-list-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink, OrdersTable, LoadingState, EmptyState, ErrorState],
  template: `
    <section class="page-section section-flush" aria-labelledby="orders-title">
      <!-- Status as filter pills, the other filters on the right. -->
      <div class="toolbar">
        <div class="filter-pills" role="group" aria-label="Show orders by status">
          <button
            type="button"
            class="filter-pill"
            [attr.aria-pressed]="!status()"
            (click)="setFilter('status', '')"
          >
            All
          </button>
          @for (s of statusOptions; track s.value) {
            <button
              type="button"
              class="filter-pill"
              [attr.aria-pressed]="s.value === status()"
              (click)="setFilter('status', s.value === status() ? '' : s.value)"
            >
              {{ s.label }}
            </button>
          }
        </div>
        <button
          type="button"
          class="btn filter-toggle"
          aria-controls="orders-filters"
          [attr.aria-expanded]="filtersOpen()"
          (click)="filtersOpen.set(!filtersOpen())"
        >
          <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true">
            <path d="M2.5 4h11M4.5 8h7M6.5 12h3" />
          </svg>
          Filter
          @if (filterCount()) {
            <span class="count-badge">{{ filterCount() }}</span>
          }
        </button>
        <form
          id="orders-filters"
          class="filters"
          role="search"
          aria-label="Filter orders"
          [class.open]="filtersOpen()"
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
            <select
              id="f-strategy"
              class="input"
              [value]="strategy() ?? ''"
              (change)="setFilter('strategy', $any($event.target).value)"
            >
              <option value="">Any strategy</option>
              @for (s of strategyOptions(); track s.value) {
                <option [value]="s.value" [selected]="s.value === strategy()">{{ s.label }}</option>
              }
            </select>
          </div>
          @if (hasFilters()) {
            <button type="button" class="btn btn-ghost clear" (click)="clearFilters()">
              Clear filters
            </button>
          }
        </form>
      </div>

      <header class="section-head">
        <h2 id="orders-title">
          Orders
          @if (page(); as p) {
            <span class="count">{{ p.total }} matching</span>
          }
        </h2>
        @if (tick()) {
          <span class="chip">
            One trading run
            <button
              type="button"
              class="chip-clear"
              aria-label="Show orders from every run"
              (click)="setFilter('tick', '')"
            >
              <svg viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true">
                <path d="M3 3l6 6M9 3l-6 6" />
              </svg>
            </button>
          </span>
        }
        <a class="text-link head-link" routerLink="/orders/ticks">See trading runs</a>
      </header>

      <div class="section-body">
        @let p = page();
        @if (orders.error(); as err) {
          <app-error-state title="Could not load orders" [error]="err" (retry)="orders.reload()" />
        } @else if (!p) {
          <app-loading-state label="Loading orders" [rows]="6" />
        } @else if (p.items.length === 0) {
          @if (hasFilters()) {
            <app-empty-state
              title="No orders match these filters"
              message="Clear a filter or widen the search."
            >
              <button type="button" class="btn" (click)="clearFilters()">Clear filters</button>
            </app-empty-state>
          } @else {
            <app-empty-state
              title="No orders yet"
              message="Orders appear here after a trading run, or when you place one by hand."
            >
              <a class="btn btn-primary" routerLink="/orders/new">New order</a>
              <a class="btn" routerLink="/orders/ticks">See trading runs</a>
            </app-empty-state>
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
      </div>
    </section>
  `,
  styleUrl: './orders-views.scss',
})
export class OrdersListPage {
  private readonly ordersApi = inject(OrdersService);
  private readonly subscriptionsApi = inject(SubscriptionsService);
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
  /** Filters set in the folded phone form, shown on its button. */
  protected readonly filterCount = computed(
    () => [this.ticker(), this.strategy()].filter(Boolean).length,
  );
  protected readonly filtersOpen = signal(false);

  /** The strategies this portfolio follows, by name. A filter from a link stays listed. */
  private readonly follows = resource({ loader: () => this.subscriptionsApi.list() });
  protected readonly strategyOptions = computed(() => {
    const picked = this.portfolioCtx.selectedId();
    const subs = this.follows.hasValue() ? this.follows.value() : [];
    const ids = new Set(
      subs
        .filter((s) => !picked || !s.portfolio_id || s.portfolio_id === picked)
        .map((s) => s.strategy_id),
    );
    const current = this.strategy();
    if (current) ids.add(current);
    return [...ids]
      .map((id) => ({ value: id, label: strategyDisplayName(id) }))
      .sort((a, b) => a.label.localeCompare(b.label));
  });
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
