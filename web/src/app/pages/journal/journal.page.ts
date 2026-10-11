import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  input,
  linkedSignal,
} from '@angular/core';
import { ActivatedRoute, Router } from '@angular/router';

import { PortfolioContextService } from '../../core/portfolio/portfolio-context.service';
import { NoBook, bookState } from '../../shared/ui/no-book';
import { PageHeader } from '../../shared/ui/page-header';
import { type PageTab, PageTabs } from '../../shared/ui/page-tabs';
import { LoadingState } from '../../shared/ui/states';
import { OrdersTabs } from '../orders/orders-tabs';
import { JournalCalendar } from './journal-calendar';
import { JournalPlaybooks } from './journal-playbooks';
import { JournalResults } from './journal-results';
import { JournalTrades } from './journal-trades';

export type JournalView = 'trades' | 'calendar' | 'results' | 'playbooks';

const VIEWS: readonly { value: JournalView; label: string }[] = [
  { value: 'trades', label: 'Trades' },
  { value: 'calendar', label: 'Calendar' },
  { value: 'results', label: 'Results' },
  { value: 'playbooks', label: 'Playbooks' },
];

/** The views as a second tab row under the Orders tabs. */
const VIEW_TABS: readonly PageTab[] = VIEWS.map((v) => ({ id: v.value, label: v.label }));

/**
 * The round-trip journal (roadmap 23.3): trades built from fills, the P&L
 * calendar, results split by what you wrote about each trade, and your
 * playbooks. Shares the Orders tab bar, with its own views as a second,
 * smaller tab row under it. The view is kept in `?view=`.
 */
@Component({
  selector: 'app-journal-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    PageHeader,
    OrdersTabs,
    PageTabs,
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
        <app-page-tabs
          #viewBar
          variant="sub"
          idPrefix="journal"
          label="Journal view"
          [tabs]="viewTabs"
          [selected]="shown()"
          (selectedChange)="pickView($event)"
        />
        <div
          role="tabpanel"
          [id]="viewBar.panelId(shown())"
          [attr.aria-labelledby]="viewBar.tabId(shown())"
        >
          @switch (shown()) {
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
        </div>
      }
      @case ('none') {
        <app-no-book message="Your journal fills in once you have a portfolio and it has traded." />
      }
      @default {
        <app-loading-state label="Loading your portfolios" [rows]="4" />
      }
    }
  `,
})
export class JournalPage {
  private readonly portfolioCtx = inject(PortfolioContextService);
  protected readonly book = computed(() => bookState(this.portfolioCtx));
  protected readonly viewTabs = VIEW_TABS;
  private readonly router = inject(Router);
  private readonly route = inject(ActivatedRoute);
  /** `?view=` so a view, such as Playbooks, can be linked to. */
  readonly view = input<string | undefined>(undefined);
  protected readonly shown = linkedSignal<JournalView>(() => journalView(this.view()));

  protected pickView(id: string | null): void {
    this.setView(journalView(id));
  }

  protected setView(view: JournalView): void {
    this.shown.set(view);
    void this.router.navigate([], {
      relativeTo: this.route,
      queryParams: { view: view === 'trades' ? null : view },
      queryParamsHandling: 'merge',
      replaceUrl: true,
    });
  }
}

/** The view a `?view=` names, or Trades. */
export function journalView(param: string | null | undefined): JournalView {
  return VIEWS.some((v) => v.value === param) ? (param as JournalView) : 'trades';
}
