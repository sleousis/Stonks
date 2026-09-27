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
import type { DividendEvent, EarningsEvent, EconomicEvent } from '../../api/models';
import { SessionService } from '../../core/auth/session.service';
import { isoDay } from '../../core/format/format';
import { WatchlistContextService } from '../../core/watchlists/watchlist-context.service';
import { DataTable, TableCell, type TableColumn } from '../../shared/ui/data-table/data-table';
import { PageHeader } from '../../shared/ui/page-header';
import { type SegmentOption, Segmented } from '../../shared/ui/segmented';
import { EmptyState, ErrorState, LoadingState } from '../../shared/ui/states';
import {
  type CalendarScope,
  addDays,
  comparisonLabel,
  importanceLabel,
  parseCountries,
  parseTickers,
  scopeQuery,
  timingLabel,
  windowError,
} from './calendar-view';
import { NewsPanel } from './news-panel';

type Tab = 'earnings' | 'dividends' | 'economic' | 'news';

const SCOPES: SegmentOption<CalendarScope>[] = [
  { value: 'holdings', label: 'Your holdings' },
  { value: 'watchlists', label: 'Watchlists' },
  { value: 'tickers', label: 'Tickers' },
  { value: 'all', label: 'Everything' },
];

/** Days shown by default, from today. */
const DEFAULT_DAYS = 30;

/**
 * Earnings, ex-dividend dates and economic releases for your holdings, a
 * watchlist, some tickers or the whole market, and the news on them.
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
    DataTable,
    TableCell,
    LoadingState,
    ErrorState,
    EmptyState,
    NewsPanel,
  ],
  template: `
    <app-page-header
      title="Calendar"
      description="Earnings, ex-dividend dates and economic releases for what you hold or watch, and the news on them."
    />

    <section class="panel filters" aria-labelledby="cal-filter-title">
      <div class="panel-head">
        <h2 id="cal-filter-title">Whose events</h2>
      </div>
      <form class="panel-body form" novalidate (submit)="$event.preventDefault(); apply()">
        <app-segmented
          label="Whose events"
          [options]="scopes"
          [value]="scope()"
          (valueChange)="scope.set($any($event))"
        />

        @if (scope() === 'watchlists') {
          <div class="field">
            <label for="cal-watchlist">Watchlist</label>
            <select
              id="cal-watchlist"
              class="input"
              (change)="watchlistId.set($any($event.target).value || null)"
            >
              <option value="" [selected]="!watchlistId()">All your watchlists</option>
              @for (w of watchlists.lists(); track w.id) {
                <option [value]="w.id" [selected]="watchlistId() === w.id">{{ w.name }}</option>
              }
            </select>
          </div>
        } @else if (scope() === 'tickers') {
          <div class="field">
            <label for="cal-tickers">Tickers</label>
            <input
              id="cal-tickers"
              class="input"
              autocapitalize="characters"
              spellcheck="false"
              placeholder="AAPL.US, MSFT.US"
              aria-describedby="cal-tickers-hint"
              [value]="tickersText()"
              (input)="tickersText.set($any($event.target).value)"
              (change)="apply()"
            />
            <span id="cal-tickers-hint" class="hint">Separate with commas or spaces.</span>
          </div>
        } @else if (scope() === 'holdings') {
          <p class="hint">What your portfolios hold now.</p>
        } @else {
          <p class="hint">Every event we have. Economic releases show for every choice.</p>
        }

        <div class="dates">
          <div class="field">
            <label for="cal-start">From</label>
            <input
              id="cal-start"
              class="input"
              type="date"
              [value]="start()"
              (change)="start.set($any($event.target).value)"
            />
          </div>
          <div class="field">
            <label for="cal-end">To</label>
            <input
              id="cal-end"
              class="input"
              type="date"
              [value]="end()"
              (change)="end.set($any($event.target).value)"
            />
          </div>
        </div>
        @if (rangeError(); as e) {
          <p class="error" role="alert">{{ e }}</p>
        }
        @if (scope() === 'tickers') {
          <div class="actions">
            <button type="submit" class="btn">Show events</button>
          </div>
        }
      </form>
    </section>

    <div class="tabs">
      <app-segmented
        label="Show"
        [options]="tabOptions()"
        [value]="tab()"
        (valueChange)="tab.set($any($event))"
      />
    </div>

    @if (tab() === 'news') {
      <app-news-panel [query]="newsQuery()" />
    } @else {
      <section class="panel" aria-labelledby="cal-events-title">
        <div class="panel-head">
          <h2 id="cal-events-title">{{ tabTitle() }}</h2>
        </div>
        @if (tab() === 'economic') {
          <form
            class="panel-body countries"
            novalidate
            (submit)="$event.preventDefault(); applyCountries()"
          >
            <div class="field">
              <label for="cal-countries">Countries</label>
              <input
                id="cal-countries"
                class="input"
                autocapitalize="characters"
                spellcheck="false"
                placeholder="US, DE, EU"
                aria-describedby="cal-countries-hint"
                [value]="countriesText()"
                (input)="countriesText.set($any($event.target).value)"
                (change)="applyCountries()"
              />
              <span id="cal-countries-hint" class="hint"
                >Two-letter codes. Leave empty for every country.</span
              >
            </div>
          </form>
        }
        @if (!query()) {
          <app-empty-state
            title="Name some tickers"
            message="Type the tickers whose events you want, then show them."
          />
        } @else if (calendar.error(); as err) {
          <app-error-state
            title="Could not load the calendar"
            [error]="err"
            (retry)="calendar.reload()"
          />
        } @else if (!calendar.hasValue()) {
          <app-loading-state label="Loading the calendar" [rows]="5" />
        } @else {
          @if (calendar.value().truncated) {
            <p class="note" role="status">
              Only the first events show. Pick fewer days or fewer tickers to see them all.
            </p>
          }
          @switch (tab()) {
            @case ('earnings') {
              @if (calendar.value().earnings.length === 0) {
                <app-empty-state title="No earnings in these days" [message]="emptyMessage()" />
              } @else {
                <app-data-table
                  caption="Earnings reports"
                  [rows]="calendar.value().earnings"
                  [columns]="earningsColumns"
                  [rowKey]="earningsKey"
                  [initialSort]="{ key: 'report_date', dir: 'asc' }"
                  [pageSize]="50"
                >
                  <ng-template appCell="ticker" [appCellOf]="calendar.value().earnings" let-e>
                    <a [routerLink]="['/charts', e.ticker]">{{ e.ticker }}</a>
                  </ng-template>
                </app-data-table>
              }
            }
            @case ('dividends') {
              @if (calendar.value().dividends.length === 0) {
                <app-empty-state
                  title="No ex-dividend dates in these days"
                  [message]="emptyMessage()"
                />
              } @else {
                <app-data-table
                  caption="Ex-dividend dates"
                  [rows]="calendar.value().dividends"
                  [columns]="dividendColumns"
                  [rowKey]="dividendKey"
                  [initialSort]="{ key: 'ex_date', dir: 'asc' }"
                  [pageSize]="50"
                >
                  <ng-template appCell="ticker" [appCellOf]="calendar.value().dividends" let-d>
                    <a [routerLink]="['/charts', d.ticker]">{{ d.ticker }}</a>
                  </ng-template>
                </app-data-table>
              }
            }
            @case ('economic') {
              @if (calendar.value().economic.length === 0) {
                <app-empty-state
                  title="No economic releases in these days"
                  message="Releases arrive with the morning calendar update. Try more days or other countries."
                />
              } @else {
                <app-data-table
                  caption="Economic releases"
                  [rows]="calendar.value().economic"
                  [columns]="economicColumns"
                  [rowKey]="economicKey"
                  [initialSort]="{ key: 'event_time', dir: 'asc' }"
                  [pageSize]="50"
                />
              }
            }
          }
        }
      </section>
    }
  `,
  styles: `
    @use 'breakpoints' as bp;
    :host {
      display: grid;
      gap: var(--space-4);
      min-width: 0;
    }
    .form {
      display: grid;
      gap: var(--space-3);
    }
    .dates {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: var(--space-3);
      max-width: 26rem;
    }
    .field select {
      max-width: 20rem;
    }
    .hint {
      font-size: var(--text-xs);
      color: var(--color-ink-3);
    }
    .error {
      font-size: var(--text-sm);
      color: var(--color-loss);
    }
    .actions {
      display: flex;
      flex-wrap: wrap;
      gap: var(--space-2);
    }
    .countries {
      max-width: 26rem;
    }
    .note {
      margin: 0 var(--space-4) var(--space-3);
      padding: var(--space-2) var(--space-3);
      border-left: 3px solid var(--color-warn);
      border-radius: var(--radius-sm);
      background: var(--color-warn-soft);
      font-size: var(--text-sm);
    }
  `,
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
  protected readonly start = signal(isoDay());
  protected readonly end = signal(addDays(isoDay(), DEFAULT_DAYS));
  protected readonly countriesText = signal('');
  private readonly countries = signal<readonly string[]>([]);
  protected readonly tab = signal<Tab>('earnings');

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
      if (!q || this.rangeError()) return undefined;
      const countries = this.countries();
      return {
        ...q,
        start: this.start(),
        end: this.end(),
        ...(countries.length ? { countries: countries.join(',') } : {}),
      };
    },
    loader: ({ params }) => this.api.calendar(params),
  });

  /** News has no "everything" scope. */
  protected readonly newsQuery = computed<NewsQuery | null>(() => {
    const q = this.query();
    return q && q.scope !== 'all' ? q : null;
  });

  protected readonly tabOptions = computed<SegmentOption<Tab>[]>(() => {
    const v = this.calendar.hasValue() ? this.calendar.value() : null;
    const count = (n: number | undefined) => (n === undefined ? '' : ` (${n})`);
    return [
      { value: 'earnings', label: `Earnings${count(v?.earnings.length)}` },
      { value: 'dividends', label: `Ex-dividend${count(v?.dividends.length)}` },
      { value: 'economic', label: `Economic${count(v?.economic.length)}` },
      { value: 'news', label: 'News' },
    ];
  });

  protected readonly tabTitle = computed(() => {
    switch (this.tab()) {
      case 'dividends':
        return 'Ex-dividend dates';
      case 'economic':
        return 'Economic releases';
      default:
        return 'Earnings reports';
    }
  });

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

  protected readonly earningsColumns: TableColumn<EarningsEvent>[] = [
    { key: 'report_date', label: 'Date', format: 'date' },
    { key: 'ticker', label: 'Ticker', mobile: 'title' },
    { key: 'name', label: 'Company', mobile: 'hide' },
    { key: 'when', label: 'When', value: (e) => timingLabel(e.before_after_market) },
    { key: 'period_end', label: 'Quarter ending', format: 'date', mobile: 'hide' },
    { key: 'eps_estimate', label: 'EPS estimate', format: 'number' },
    { key: 'eps_actual', label: 'EPS actual', format: 'number' },
    {
      key: 'surprise',
      label: 'Surprise',
      format: 'signedPercent',
      tone: true,
      value: (e) => (typeof e.surprise_percent === 'number' ? e.surprise_percent / 100 : null),
      mobile: 'hide',
    },
  ];

  protected readonly dividendColumns: TableColumn<DividendEvent>[] = [
    { key: 'ex_date', label: 'Ex-dividend', format: 'date' },
    { key: 'ticker', label: 'Ticker', mobile: 'title' },
    { key: 'name', label: 'Company', mobile: 'hide' },
    { key: 'amount', label: 'Amount', format: 'money', currency: (d) => d.currency },
    { key: 'record_date', label: 'Record', format: 'date', mobile: 'hide' },
    { key: 'pay_date', label: 'Paid', format: 'date' },
  ];

  protected readonly economicColumns: TableColumn<EconomicEvent>[] = [
    { key: 'event_time', label: 'Time', format: 'datetime' },
    { key: 'event_type', label: 'Release', mobile: 'title' },
    { key: 'country', label: 'Country' },
    { key: 'importance', label: 'Importance', value: (e) => importanceLabel(e.importance) },
    { key: 'period', label: 'Period', mobile: 'hide' },
    { key: 'actual', label: 'Actual', format: 'number' },
    { key: 'estimate', label: 'Estimate', format: 'number' },
    { key: 'previous', label: 'Previous', format: 'number', mobile: 'hide' },
    {
      key: 'comparison',
      label: 'Compared',
      value: (e) => comparisonLabel(e.comparison),
      mobile: 'hide',
    },
  ];

  protected readonly earningsKey = (e: EarningsEvent) => `${e.ticker}|${e.period_end}`;
  protected readonly dividendKey = (d: DividendEvent) => `${d.ticker}|${d.ex_date}`;
  protected readonly economicKey = (e: EconomicEvent) =>
    `${e.country}|${e.event_time}|${e.event_type}|${e.comparison}`;

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
      this.start.set(d);
      this.end.set(addDays(d, DEFAULT_DAYS));
    }
  }

  protected apply(): void {
    this.tickers.set(parseTickers(this.tickersText()));
  }

  protected applyCountries(): void {
    this.countries.set(parseCountries(this.countriesText()));
  }
}
