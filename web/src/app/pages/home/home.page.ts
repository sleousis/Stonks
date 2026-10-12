import { ChangeDetectionStrategy, Component, computed, inject } from '@angular/core';
import { RouterLink } from '@angular/router';

import { SessionService } from '../../core/auth/session.service';
import { dayChangeFrom, sessionPhrase } from '../../core/format/day-change';
import { formatPercent, formatTime, formatWeekday, isoDay } from '../../core/format/format';
import { PortfolioContextService } from '../../core/portfolio/portfolio-context.service';
import { nextTradingRun } from '../../core/schedule/job-labels';
import { TradingDayService } from '../../core/schedule/trading-day.service';
import { autoRefresh } from '../../shared/auto-refresh';
import { PageHeader } from '../../shared/ui/page-header';
import { WatchlistFilter } from '../../shared/ui/watchlist-filter';
import { BreadthCard } from './breadth-card';
import { FillsTape } from './fills-tape';
import { HoldingsSection } from './holdings-section';
import { HomeStore } from './home-store';
import { PortfolioCard } from './portfolio-card';
import { SetupCard } from './setup-card';
import { SignalsCard } from './signals-card';
import { StrategiesCard } from './strategies-card';
import { TotalsCard } from './totals-card';

/** The data a standfirst is built from. */
export interface StandfirstFacts {
  /** `paper`, `live` or null without a portfolio. */
  mode: 'paper' | 'live' | null;
  /** The last session's return as a fraction, or null when none yet. */
  dayReturn: number | null;
  /** "today", "on Friday" or "last session". */
  dayPhrase: string;
  /** "22:45" or "Mon 22:45", or null when nothing is scheduled. */
  nextRun: string | null;
}

/**
 * One sentence under the title, built from data (20 words at most): how
 * the portfolio moved and when the next trading run is.
 */
export function standfirst(f: StandfirstFacts): string {
  const parts: string[] = [];
  if (f.mode) {
    const book = f.mode === 'live' ? 'Your live portfolio' : 'Your paper portfolio';
    if (f.dayReturn == null) {
      parts.push(`${book} has not moved yet.`);
    } else if (f.dayReturn === 0) {
      parts.push(`${book} was flat ${f.dayPhrase}.`);
    } else {
      const verb = f.dayReturn > 0 ? 'rose' : 'fell';
      parts.push(
        `${book} ${verb} ${formatPercent(Math.abs(f.dayReturn), { digits: 2 })} ${f.dayPhrase}.`,
      );
    }
  } else {
    parts.push('What your strategies signalled, and what runs next.');
  }
  if (f.nextRun) parts.push(`The next run is at ${f.nextRun}.`);
  return parts.join(' ');
}

/**
 * Today, the trader's home (docs/ui.md, "Today"): a data-built standfirst,
 * the setup band while first-run steps are left, the overview (the value as
 * the one big figure, its curve and the facts), the fills tape, then two
 * columns: the strategies I follow and my holdings, beside the signals and
 * runs timeline and the market's breadth. Admins get the totals across
 * traders under their own portfolio (M3). On a phone the order is setup,
 * portfolio, facts, fills, strategies, holdings, signals, breadth.
 */
@Component({
  selector: 'app-home-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  providers: [HomeStore],
  imports: [
    RouterLink,
    PageHeader,
    FillsTape,
    PortfolioCard,
    HoldingsSection,
    TotalsCard,
    SignalsCard,
    StrategiesCard,
    SetupCard,
    WatchlistFilter,
    BreadthCard,
  ],
  template: `
    <app-page-header title="Today" [description]="standfirst()">
      <app-watchlist-filter actions class="show" ariaLabel="Show fills and signals for" />
    </app-page-header>

    <app-setup-card class="setup" />

    @if (session.status() === 'open') {
      <p class="banner callout" data-tone="info">
        You are not signed in. Reads work on this machine, but your portfolio and strategies need
        you to <a routerLink="/login">sign in</a>.
      </p>
    }

    <div class="today">
      <app-portfolio-card />
      @if (session.isAdmin()) {
        <app-totals-card />
      }
      <app-fills-tape />
      <div class="cols">
        <div class="section-stack">
          <app-strategies-card />
          <app-holdings-section />
        </div>
        <div class="section-stack">
          <app-signals-card />
          <app-breadth-card />
        </div>
      </div>
    </div>
  `,
  styles: `
    @use 'breakpoints' as bp;

    .today {
      display: grid;
      grid-template-columns: minmax(0, 1fr);
      gap: var(--space-6);
      min-width: 0;
    }
    .today > .cols {
      margin-top: var(--space-4);
    }
    .setup:not(:empty),
    .banner {
      display: block;
      margin-bottom: var(--space-7);

      @include bp.phone {
        margin-bottom: var(--space-6);
      }
    }
    /* Phones: the Show picker sits under the standfirst at full width. */
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
  private readonly store = inject(HomeStore);
  private readonly ctx = inject(PortfolioContextService);
  private readonly day = inject(TradingDayService);

  constructor() {
    this.store.need('portfolio', 'follows');
  }

  /** The strategy switches stay fresh too (UX-12). */
  protected readonly strategiesAuto = autoRefresh(() => [this.store.subs], {
    triggers: [this.day.runsPassed],
  });

  protected readonly standfirst = computed(() => {
    const book = this.store.portfolio.hasValue() ? this.store.portfolio.value() : null;
    const series = this.store.pnl.hasValue() ? this.store.pnl.value() : null;
    const change = dayChangeFrom(series?.day_change, series?.rows.at(-1) ?? null);
    const now = Date.now();
    const job = nextTradingRun(this.day.jobs(), now);
    let nextRun: string | null = null;
    if (job?.next_run_at) {
      const time = formatTime(job.next_run_at);
      nextRun =
        isoDay(new Date(job.next_run_at)) === isoDay(new Date(now))
          ? time
          : `${formatWeekday(job.next_run_at)} ${time}`;
    }
    return standfirst({
      mode: book ? (this.ctx.current()?.trading ?? 'paper') : null,
      dayReturn: change?.pct ?? null,
      dayPhrase: sessionPhrase(change?.day),
      nextRun,
    });
  });
}
