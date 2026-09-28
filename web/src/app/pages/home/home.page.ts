import { ChangeDetectionStrategy, Component, computed, inject, viewChild } from '@angular/core';
import { RouterLink } from '@angular/router';

import { SessionService } from '../../core/auth/session.service';
import { formatLongDay } from '../../core/format/format';
import { type Reloadable, autoRefresh } from '../../shared/auto-refresh';
import { FillsTape } from './fills-tape';
import { PageHeader } from '../../shared/ui/page-header';
import { PortfolioCard } from './portfolio-card';
import { SetupCard } from './setup-card';
import { WatchlistFilter } from '../../shared/ui/watchlist-filter';
import { SignalsCard } from './signals-card';
import { StrategiesCard } from './strategies-card';
import { TotalsCard } from './totals-card';
import { TradingDayService } from '../../core/schedule/trading-day.service';

/**
 * Today, the trader's home: my portfolio with a tape of today's fills,
 * today's signals and runs in time order, and my strategies with their
 * switches. Admins see their own portfolio first too, with the totals
 * across traders under it (M3, F23). The left column flows on its own, so
 * a tall strategies list never leaves a hole beside it.
 */
@Component({
  selector: 'app-home-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    RouterLink,
    PageHeader,
    FillsTape,
    PortfolioCard,
    TotalsCard,
    SignalsCard,
    StrategiesCard,
    SetupCard,
    WatchlistFilter,
  ],
  template: `
    <app-page-header [title]="greeting()" [description]="dateLine()">
      <app-watchlist-filter actions ariaLabel="Show fills and signals for" />
    </app-page-header>

    <app-setup-card class="setup" />

    @if (session.status() === 'open') {
      <p class="banner">
        You are not signed in. Reads work on this machine, but your portfolio and strategies need
        you to <a routerLink="/login">sign in</a>.
      </p>
    }

    <div class="home">
      <app-fills-tape class="tape" />
      <div class="portfolio">
        <app-portfolio-card />
        @if (session.isAdmin()) {
          <app-totals-card />
        }
      </div>
      <app-signals-card class="signals" />
      <app-strategies-card class="strategies" />
    </div>
  `,
  styles: `
    @use 'breakpoints' as bp;

    .home {
      display: grid;
      gap: var(--space-4);
      grid-template-columns: minmax(0, 1fr);
      grid-template-areas: 'portfolio' 'tape' 'signals' 'strategies';

      @include bp.from-desktop {
        grid-template-columns: minmax(0, 7fr) minmax(0, 5fr);
        grid-template-areas: 'tape tape' 'portfolio strategies' 'signals strategies';
        /* The last row is flexible, so a tall strategies column grows it and
           never stretches the portfolio row into a gap (M3). */
        grid-template-rows: auto auto 1fr;
        align-items: start;
      }
    }
    .tape {
      grid-area: tape;
      min-width: 0;
    }
    .portfolio {
      grid-area: portfolio;
      display: grid;
      gap: var(--space-3);
      min-width: 0;
    }
    .signals {
      grid-area: signals;
    }
    .strategies {
      grid-area: strategies;
    }
    .setup:not(:empty) {
      margin-bottom: var(--space-4);
    }
    .banner {
      margin-bottom: var(--space-4);
      padding: var(--space-2) var(--space-3);
      border-left: 3px solid var(--color-info);
      border-radius: var(--radius-sm);
      background: var(--color-info-soft);
      font-size: var(--text-sm);
    }
  `,
})
export class HomePage {
  protected readonly session = inject(SessionService);

  /**
   * The strategies card keeps its switches fresh too (UX-12). Its file
   * belongs to another page agent, so Today reloads its list from here: the
   * other cards refresh themselves the same way.
   */
  private readonly strategiesCard = viewChild(StrategiesCard);
  protected readonly strategiesAuto = autoRefresh(
    () => {
      const card = this.strategiesCard();
      return card ? [card['subs'] as Reloadable] : [];
    },
    { triggers: [inject(TradingDayService).runsPassed] },
  );

  /** "Sunday 27 September" in the trader's locale, then what the page holds. */
  protected readonly dateLine = computed(
    () => `${formatLongDay(new Date())}. What ran, what it decided, what filled and what is next.`,
  );

  protected readonly greeting = computed(() => {
    const name = this.session.me()?.display_name;
    return name ? `Hello, ${name}` : 'Today';
  });
}
