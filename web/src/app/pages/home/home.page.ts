import { ChangeDetectionStrategy, Component, computed, inject, viewChild } from '@angular/core';
import { RouterLink } from '@angular/router';

import { SessionService } from '../../core/auth/session.service';
import { formatLongDay } from '../../core/format/format';
import { type Reloadable, autoRefresh } from '../../shared/auto-refresh';
import { BreadthCard } from './breadth-card';
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
 * switches, and the market's breadth. Admins see their own portfolio first too, with the totals
 * across traders under it (M3, F23). The left column flows on its own, so
 * a tall strategies list never leaves a hole beside it. On a phone the
 * order is tape, portfolio, strategies, signals, breadth.
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
    BreadthCard,
  ],
  template: `
    <app-page-header [title]="greeting()" [description]="dateLine()">
      <app-watchlist-filter actions class="show" ariaLabel="Show fills and signals for" />
    </app-page-header>

    <app-setup-card class="setup" />

    @if (session.status() === 'open') {
      <p class="banner callout" data-tone="info">
        You are not signed in. Reads work on this machine, but your portfolio and strategies need
        you to <a routerLink="/login">sign in</a>.
      </p>
    }

    <div class="home">
      <app-fills-tape class="tape" />
      <div class="col left">
        <div class="portfolio">
          <app-portfolio-card />
          @if (session.isAdmin()) {
            <app-totals-card />
          }
        </div>
        <app-signals-card class="signals" />
      </div>
      <div class="col right">
        <app-strategies-card class="strategies" />
        <app-breadth-card class="breadth" />
      </div>
    </div>
  `,
  styles: `
    @use 'breakpoints' as bp;

    .home {
      display: grid;
      gap: var(--space-4);

      @include bp.phone {
        gap: var(--space-3);
      }
      grid-template-columns: minmax(0, 1fr);
      /* Phones: following a strategy comes before the signals it sends (FR-05). */
      grid-template-areas: 'tape' 'portfolio' 'strategies' 'signals' 'breadth';

      /* Desktop: two columns that each flow on their own, so a tall card in
         one never leaves a gap in the other (M3, FR-10). */
      @include bp.from-desktop {
        grid-template-columns: minmax(0, 7fr) minmax(0, 5fr);
        grid-template-areas:
          'tape tape'
          'left right';
        align-items: start;
      }
    }
    /* On a phone the columns step aside and their cards join the one grid. */
    .col {
      display: contents;

      @include bp.from-desktop {
        display: flex;
        flex-direction: column;
        gap: var(--space-4);
        min-width: 0;
      }
    }
    .left {
      grid-area: left;
    }
    .right {
      grid-area: right;
    }
    .tape {
      grid-area: tape;
      min-width: 0;
    }
    .portfolio {
      grid-area: portfolio;
      display: grid;
      gap: var(--space-4);
      min-width: 0;

      @include bp.phone {
        gap: var(--space-3);
      }
    }
    .signals {
      grid-area: signals;
    }
    .strategies {
      grid-area: strategies;
    }
    .breadth {
      grid-area: breadth;
    }
    .setup:not(:empty),
    .banner {
      display: block;
      margin-bottom: var(--space-4);

      @include bp.phone {
        margin-bottom: var(--space-3);
      }
    }
    /* Phones: the Show picker sits under the description at full width. */
    @include bp.phone {
      .show {
        display: block;
        width: 100%;
      }
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
