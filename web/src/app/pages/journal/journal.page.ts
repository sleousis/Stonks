import { ChangeDetectionStrategy, Component, computed, inject, signal } from '@angular/core';

import { PortfolioContextService } from '../../core/portfolio/portfolio-context.service';
import { NoBook, bookState } from '../../shared/ui/no-book';
import { PageHeader } from '../../shared/ui/page-header';
import { Segmented, type SegmentOption } from '../../shared/ui/segmented';
import { LoadingState } from '../../shared/ui/states';
import { OrdersTabs } from '../orders/orders-tabs';
import { JournalCalendar } from './journal-calendar';
import { JournalPlaybooks } from './journal-playbooks';
import { JournalResults } from './journal-results';
import { JournalTrades } from './journal-trades';

export type JournalView = 'trades' | 'calendar' | 'results' | 'playbooks';

const VIEWS: readonly SegmentOption<JournalView>[] = [
  { value: 'trades', label: 'Trades' },
  { value: 'calendar', label: 'Calendar' },
  { value: 'results', label: 'Results' },
  { value: 'playbooks', label: 'Playbooks' },
];

/**
 * The round-trip journal (roadmap 23.3): trades built from fills, the P&L
 * calendar, results split by what you wrote about each trade, and your
 * playbooks. Shares the Orders tab bar.
 */
@Component({
  selector: 'app-journal-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    PageHeader,
    OrdersTabs,
    Segmented,
    NoBook,
    LoadingState,
    JournalTrades,
    JournalCalendar,
    JournalResults,
    JournalPlaybooks,
  ],
  template: `
    <app-page-header
      title="Journal"
      description="Every trade from entry to exit, how far it went for and against you, and what you learned."
    />
    <app-orders-tabs />
    @switch (book()) {
      @case ('ready') {
        <div class="views">
          <app-segmented label="Journal view" [options]="views" [(value)]="view" />
        </div>
        @switch (view()) {
          @case ('trades') {
            <app-journal-trades />
          }
          @case ('calendar') {
            <app-journal-calendar />
          }
          @case ('results') {
            <app-journal-results />
          }
          @case ('playbooks') {
            <app-journal-playbooks />
          }
        }
      }
      @case ('none') {
        <app-no-book message="Your journal fills in once you have a portfolio and it has traded." />
      }
      @default {
        <app-loading-state label="Loading your portfolios" [rows]="4" />
      }
    }
  `,
  styles: `
    .views {
      margin-bottom: var(--space-4);
    }
  `,
})
export class JournalPage {
  private readonly portfolioCtx = inject(PortfolioContextService);
  protected readonly book = computed(() => bookState(this.portfolioCtx));
  protected readonly views = VIEWS;
  readonly view = signal<JournalView>('trades');
}
