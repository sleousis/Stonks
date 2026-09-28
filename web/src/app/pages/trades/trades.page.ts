import { ChangeDetectionStrategy, Component, computed, inject } from '@angular/core';

import { PortfolioContextService } from '../../core/portfolio/portfolio-context.service';
import { ExportButton } from '../../shared/ui/export-button';
import { NoBook, bookState } from '../../shared/ui/no-book';
import { PageHeader } from '../../shared/ui/page-header';
import { LoadingState } from '../../shared/ui/states';
import { OrdersTabs } from '../orders/orders-tabs';
import { TcaSummary } from './tca-summary';
import { TradeJournal } from './trade-journal';

/**
 * Trade costs: what trading cost (implementation shortfall) and the trade
 * journal. Shares the Orders tab bar, so orders and their costs are one tap
 * apart. With no portfolio it points at opening one and reads nothing (UX-13).
 */
@Component({
  selector: 'app-trades-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [PageHeader, OrdersTabs, TcaSummary, TradeJournal, ExportButton, NoBook, LoadingState],
  template: `
    <!-- M2: the Orders title stays on every Orders tab; this tab is Trade costs. -->
    <app-page-header
      title="Orders"
      description="Trade costs: what your orders cost against the price when they were decided, and a journal of every trade."
    >
      @if (book() === 'ready') {
        <app-export-button actions kind="journal" label="Journal CSV" [ghost]="true" />
      }
    </app-page-header>
    <app-orders-tabs />
    @switch (book()) {
      @case ('ready') {
        <app-tca-summary />
        <app-trade-journal class="journal" />
      }
      @case ('none') {
        <app-no-book
          message="What your trades cost shows here once you have a portfolio and it has traded."
        />
      }
      @default {
        <app-loading-state label="Loading your portfolios" [rows]="4" />
      }
    }
  `,
  styles: `
    .journal {
      margin-top: var(--space-4);
    }
  `,
})
export class TradesPage {
  private readonly portfolioCtx = inject(PortfolioContextService);
  protected readonly book = computed(() => bookState(this.portfolioCtx));
}
