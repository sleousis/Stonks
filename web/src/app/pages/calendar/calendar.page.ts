import {
  ChangeDetectionStrategy,
  Component,
  type OnInit,
  computed,
  inject,
  input,
  resource,
  signal,
} from '@angular/core';
import { RouterLink } from '@angular/router';

import { CalendarsService, type NewsQuery } from '../../api/calendars.service';
import type { DividendEvent, EarningsEvent, EconomicEvent, FilingEvent } from '../../api/models';
import { SessionService } from '../../core/auth/session.service';
import { formatDate, isoDay } from '../../core/format/format';
import { WatchlistContextService } from '../../core/watchlists/watchlist-context.service';
import { DataPlanNote } from '../../shared/ui/data-plan-note';
import { PageHeader } from '../../shared/ui/page-header';
import { type SegmentOption, Segmented } from '../../shared/ui/segmented';
import { EmptyState, ErrorState, LoadingState } from '../../shared/ui/states';
import {
  type AgendaDay,
  type AgendaItem,
  type CalendarScope,
  KIND_LABELS,
  addDays,
  agendaDayLabel,
  dividendItem,
  earningsItem,
  economicItem,
  filingItem,
  groupByDay,
  parseCountries,
  parseTickers,
  relativeDay,
  scopeQuery,
  windowError,
} from './calendar-view';
import { NewsPanel } from './news-panel';

type Tab = 'all' | 'earnings' | 'dividends' | 'economic' | 'filings' | 'news';

/** Rows shown before "Show more". */
const ROW_STEP = 120;

const SCOPES: SegmentOption<CalendarScope>[] = [
  { value: 'holdings', label: 'Your holdings' },
  { value: 'watchlists', label: 'Watchlists' },
  { value: 'tickers', label: 'Tickers' },
  { value: 'all', label: 'Everything' },
];

/** Days shown by default: the next 30, or the last 30 for filings. */
const DEFAULT_DAYS = 30;

/** A From and To pair. */
interface DayRange {
  start: string;
  end: string;
}

/**
 * Earnings, ex-dividend dates and economic releases for your holdings, a
 * watchlist, some tickers or the whole market, the SEC filings (8-K) of
 * those companies with a link to each document, and the news on them.
 *
 * `?ticker=&date=` (the event alerts link here) show one ticker from that day.
 * `?country=&date=` (the economic release alerts) show that country's releases.
 */
@Component({
  selector: 'app-calendar-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    RouterLink,
    PageHeader,
    Segmented,
    LoadingState,
    ErrorState,
    EmptyState,
    NewsPanel,
    DataPlanNote,
  ],
  templateUrl: './calendar.page.html',
  styleUrl: './calendar.page.scss',
})
export class CalendarPage implements OnInit {
  private readonly api = inject(CalendarsService);
  protected readonly watchlists = inject(WatchlistContextService);

  /** Deep link from an event alert: one ticker, from that day. */
  readonly ticker = input<string>();
  readonly date = input<string>();
  /** Deep link from an economic release alert: that country's releases. */
  readonly country = input<string>();

  protected readonly scopes = SCOPES;
  protected readonly scope = signal<CalendarScope>('holdings');
  protected readonly watchlistId = signal<string | null>(null);
  /** What is typed; `tickers` is what was last shown. */
  protected readonly tickersText = signal('');
  private readonly tickers = signal<readonly string[]>([]);
  /** The window of earnings, dividends and releases: the next 30 days. */
  private readonly ahead = signal<DayRange>({
    start: isoDay(),
    end: addDays(isoDay(), DEFAULT_DAYS),
  });
  /** The window of filings, which are past events: the last 30 days. */
  private readonly past = signal<DayRange>({
    start: addDays(isoDay(), -DEFAULT_DAYS),
    end: isoDay(),
  });
  /** From and To show the window of the open tab. */
  private readonly range = computed(() => (this.tab() === 'filings' ? this.past() : this.ahead()));
  protected readonly start = computed(() => this.range().start);
  protected readonly end = computed(() => this.range().end);
  protected readonly countriesText = signal('');
  private readonly countries = signal<readonly string[]>([]);
  protected readonly tab = signal<Tab>('all');
  /** How many agenda rows show; "Show more" adds a step. */
  protected readonly limit = signal(ROW_STEP);

  protected readonly rangeError = computed(() => windowError(this.start(), this.end()));

  /** The scope as the API takes it, or null when it cannot be asked yet. */
  protected readonly query = computed(() =>
    scopeQuery({
      scope: this.scope(),
      watchlistId: this.watchlistId(),
      tickers: this.tickers(),
    }),
  );

  protected readonly calendar = resource({
    params: () => {
      const q = this.query();
      const { start, end } = this.ahead();
      if (!q || windowError(start, end)) return undefined;
      const countries = this.countries();
      return {
        ...q,
        start,
        end,
        ...(countries.length ? { countries: countries.join(',') } : {}),
      };
    },
    loader: ({ params }) => this.api.calendar(params),
  });

  /** Filings over the past window, read once the Filings tab opens. */
  protected readonly filingsCalendar = resource({
    params: () => {
      const q = this.query();
      const { start, end } = this.past();
      if (this.tab() !== 'filings' || !q || windowError(start, end)) return undefined;
      return { ...q, start, end };
    },
    loader: ({ params }) => this.api.calendar(params),
  });

  /** The read behind the open tab. */
  protected readonly shown = computed(() =>
    this.tab() === 'filings' ? this.filingsCalendar : this.calendar,
  );

  /** News has no "everything" scope. */
  protected readonly newsQuery = computed<NewsQuery | null>(() => {
    const q = this.query();
    return q && q.scope !== 'all' ? q : null;
  });

  protected readonly tabOptions = computed<SegmentOption<Tab>[]>(() => {
    const v = this.calendar.hasValue() ? this.calendar.value() : null;
    const count = (n: number | undefined) => (n === undefined ? '' : ` (${n})`);
    return [
      {
        value: 'all',
        label: `All${count(v ? v.earnings.length + v.dividends.length + v.economic.length : undefined)}`,
      },
      { value: 'earnings', label: `Earnings${count(v?.earnings.length)}` },
      { value: 'dividends', label: `Ex-dividend${count(v?.dividends.length)}` },
      { value: 'economic', label: `Economic${count(v?.economic.length)}` },
      { value: 'filings', label: `Filings${count(this.filingsCalendar.value()?.filings?.length)}` },
      { value: 'news', label: 'News' },
    ];
  });

  protected readonly tabTitle = computed(() => {
    switch (this.tab()) {
      case 'dividends':
        return 'Ex-dividend dates';
      case 'economic':
        return 'Economic releases';
      case 'filings':
        return 'SEC filings';
      case 'earnings':
        return 'Earnings reports';
      default:
        return 'Coming up';
    }
  });

  /** The events of the open tab, grouped by day. */
  protected readonly agenda = computed<AgendaDay[]>(() => {
    const read = this.shown();
    if (!read.hasValue()) return [];
    const v = read.value();
    const tab = this.tab();
    const items: AgendaItem[] = [];
    if (tab === 'all' || tab === 'earnings') items.push(...v.earnings.map(earningsItem));
    if (tab === 'all' || tab === 'dividends') items.push(...v.dividends.map(dividendItem));
    if (tab === 'all' || tab === 'economic') items.push(...v.economic.map(economicItem));
    if (tab === 'filings') items.push(...(v.filings ?? []).map(filingItem));
    return groupByDay(items, tab === 'filings');
  });

  protected readonly eventCount = computed(() =>
    this.agenda().reduce((n, d) => n + d.items.length, 0),
  );

  /** The days shown, cut after `limit` rows. */
  protected readonly shownDays = computed<AgendaDay[]>(() => {
    let left = this.limit();
    const out: AgendaDay[] = [];
    for (const d of this.agenda()) {
      if (left <= 0) break;
      out.push(d.items.length <= left ? d : { day: d.day, items: d.items.slice(0, left) });
      left -= d.items.length;
    }
    return out;
  });

  /** "3 earnings reports, 1 ex-dividend date and 4 releases from 2026-10-12 to 2026-11-11." */
  protected readonly standfirst = computed(() => {
    const base = 'Earnings, ex-dividend dates, economic releases and SEC filings for what you hold or watch.';
    if (!this.calendar.hasValue()) return base;
    const v = this.calendar.value();
    const plural = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`;
    const parts = [
      plural(v.earnings.length, 'earnings report', 'earnings reports'),
      plural(v.dividends.length, 'ex-dividend date', 'ex-dividend dates'),
      plural(v.economic.length, 'economic release', 'economic releases'),
    ];
    const { start, end } = this.ahead();
    return `${parts[0]}, ${parts[1]} and ${parts[2]} from ${formatDate(start)} to ${formatDate(end)}.`;
  });

  protected readonly kindLabels = KIND_LABELS;
  protected readonly dayLabel = agendaDayLabel;
  protected readonly relative = (day: string) => relativeDay(day);

  protected showMore(): void {
    this.limit.update((n) => n + ROW_STEP);
  }

  protected pickTab(tab: Tab): void {
    this.tab.set(tab);
    this.limit.set(ROW_STEP);
  }

  protected readonly emptyMessage = computed(() => {
    switch (this.scope()) {
      case 'holdings':
        return 'Nothing you hold has one coming. Try more days, or a watchlist.';
      case 'watchlists':
        return 'Nothing you watch has one coming. Try more days.';
      case 'tickers':
        return 'These tickers have none in these days. Try more days.';
      default:
        return 'The calendars fill each morning. Try more days.';
    }
  });

  /** Filings are past events, and follow named companies only. */
  protected readonly filingsEmptyMessage = computed(() =>
    this.scope() === 'all'
      ? 'Filings show for your holdings, a watchlist or the tickers you name.'
      : 'Filings are past events: pick an earlier From date to see what these companies filed.',
  );

  constructor() {
    // Watchlists are personal: nothing to read for someone not signed in.
    // Read them fresh, so a list made since the app opened is offered.
    if (inject(SessionService).signedIn()) void this.watchlists.load(true);
  }

  ngOnInit(): void {
    const t = this.ticker();
    if (t) {
      const tickers = parseTickers(t);
      this.scope.set('tickers');
      this.tickersText.set(tickers.join(', '));
      this.tickers.set(tickers);
    }
    const countries = parseCountries(this.country() ?? '');
    if (countries.length) {
      this.tab.set('economic');
      this.countriesText.set(countries.join(', '));
      this.countries.set(countries);
    }
    const d = this.date();
    if (d && /^\d{4}-\d{2}-\d{2}$/.test(d)) {
      this.ahead.set({ start: d, end: addDays(d, DEFAULT_DAYS) });
    }
  }

  /** A typed From or To, for the window of the open tab. */
  protected setRange(change: Partial<DayRange>): void {
    const window = this.tab() === 'filings' ? this.past : this.ahead;
    window.update((r) => ({ ...r, ...change }));
  }

  protected apply(): void {
    this.tickers.set(parseTickers(this.tickersText()));
  }

  protected applyCountries(): void {
    this.countries.set(parseCountries(this.countriesText()));
  }
}
