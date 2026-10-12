import { ChangeDetectionStrategy, Component, computed, inject } from '@angular/core';

import { PortfolioContextService } from '../../core/portfolio/portfolio-context.service';
import { ExportButton } from '../../shared/ui/export-button';
import { NoBook, bookState } from '../../shared/ui/no-book';
import { PageHeader } from '../../shared/ui/page-header';
import { LoadingState } from '../../shared/ui/states';
import { TcaSummary } from './tca-summary';
import { TradeJournal } from './trade-journal';

/**
 * Trade costs: what trading cost (implementation shortfall) and the trade
 * journal, a tab of its own in the Trade workspace. With no portfolio it points at opening one and reads nothing (UX-13).
 */
@Component({
  selector: 'app-trades-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [PageHeader, TcaSummary, TradeJournal, ExportButton, NoBook, LoadingState],
  template: `
    <app-page-header
      title="Trade costs"
      description="What your orders cost against the price when they were decided, and a journal of every trade."
    >
      @if (book() === 'ready') {
        <app-export-button actions kind="journal" label="Journal CSV" [ghost]="true" />
      }
    </app-page-header>
    @switch (book()) {
      @case ('ready') {
        <div class="section-stack">
          <app-tca-summary />
          <app-trade-journal />
        </div>
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

})
export class TradesPage {
  private readonly portfolioCtx = inject(PortfolioContextService);
  protected readonly book = computed(() => bookState(this.portfolioCtx));
}
