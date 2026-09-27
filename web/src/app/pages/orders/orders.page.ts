import { ChangeDetectionStrategy, Component, computed, inject } from '@angular/core';
import { toSignal } from '@angular/core/rxjs-interop';
import { NavigationEnd, Router, RouterLink, RouterOutlet } from '@angular/router';
import { filter, map } from 'rxjs';

import { RefreshStatus, UpdatedAgo } from '../../shared/auto-refresh';
import { ExportButton } from '../../shared/ui/export-button';
import { PortfolioContextService } from '../../core/portfolio/portfolio-context.service';
import { NoBook, bookState } from '../../shared/ui/no-book';
import { PageHeader } from '../../shared/ui/page-header';
import { LoadingState } from '../../shared/ui/states';
import { OrdersTabs } from './orders-tabs';

/**
 * Orders and the trading runs that produce them. The header and view tabs
 * (shared with Trade costs) stay put; the child routes (orders, fills, runs,
 * runs/:id) render below and report their freshness to the header through
 * RefreshStatus. With no portfolio, Orders and Fills point at opening one.
 */
@Component({
  selector: 'app-orders-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    RouterOutlet,
    RouterLink,
    PageHeader,
    UpdatedAgo,
    ExportButton,
    OrdersTabs,
    NoBook,
    LoadingState,
  ],
  providers: [RefreshStatus],
  template: `
    <app-page-header
      title="Orders"
      description="Orders placed by trading runs, the fills they received, and the runs themselves."
    >
      <app-updated-ago [at]="status.updatedAt()" />
      @if (!onRuns() && book() !== 'none') {
        <ng-container ngProjectAs="[actions]">
          <app-export-button kind="orders" label="Orders CSV" [ghost]="true" />
          <app-export-button kind="fills" label="Fills CSV" [ghost]="true" />
          <a class="btn" routerLink="/orders/ticks">Go to trading runs</a>
        </ng-container>
      }
    </app-page-header>

    <app-orders-tabs />

    @if (onRuns() || book() === 'ready') {
      <router-outlet />
    } @else if (book() === 'none') {
      <app-no-book
        message="Orders and fills show here once you have a portfolio. A paper one trades with pretend money."
      />
    } @else {
      <app-loading-state label="Loading your portfolios" [rows]="4" />
    }
  `,
})
export class OrdersPage {
  protected readonly status = inject(RefreshStatus);
  private readonly portfolioCtx = inject(PortfolioContextService);
  /** Orders and fills belong to a portfolio: with none, point at opening one (UX-13). */
  protected readonly book = computed(() => bookState(this.portfolioCtx));
  private readonly router = inject(Router);
  private readonly url = toSignal(
    this.router.events.pipe(
      filter((e) => e instanceof NavigationEnd),
      map(() => this.router.url),
    ),
    { initialValue: this.router.url },
  );
  /** On the runs tab the runner is already on screen, so the header link goes. */
  protected readonly onRuns = computed(() => this.url().startsWith('/orders/ticks'));
}
