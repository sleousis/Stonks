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
import { isoDay } from '../../core/format/format';
import { WatchlistContextService } from '../../core/watchlists/watchlist-context.service';
import { DataTable, TableCell, type TableColumn } from '../../shared/ui/data-table/data-table';
import { DataPlanNote } from '../../shared/ui/data-plan-note';
import { PageHeader } from '../../shared/ui/page-header';
import { type SegmentOption, Segmented } from '../../shared/ui/segmented';
import { EmptyState, ErrorState, LoadingState } from '../../shared/ui/states';
import {
  type CalendarScope,
  addDays,
  economicRelease,
  economicValue,
  filingItemsLabel,
  importanceLabel,
  parseCountries,
  parseTickers,
  safeUrl,
  scopeQuery,
  timingLabel,
  windowError,
} from './calendar-view';
import { NewsPanel } from './news-panel';

type Tab = 'earnings' | 'dividends' | 'economic' | 'filings' | 'news';

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

/** An economic figure with its unit: "2.5%", "210K jobs". Sorts by the number. */
function economicFigure(
  key: 'actual' | 'estimate' | 'previous',
  label: string,
): TableColumn<EconomicEvent> {
  return {
    key,
    label,
    format: 'number',
    help: false,
    display: (e) => economicValue(e[key], e),
  };
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
    DataTable,
    TableCell,
    LoadingState,
    ErrorState,
    EmptyState,
    NewsPanel,
    DataPlanNote,
  ],
  template: `
    <app-page-header
      title="Calendar"
      description="Earnings, ex-dividend dates, economic releases and SEC filings for what you hold or watch, and the news on them."
    />
    <app-data-plan-note kind="calendars" />

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
              class="input num"
              type="date"
              [value]="start()"
              (change)="setRange({ start: $any($event.target).value })"
            />
          </div>
          <div class="field">
            <label for="cal-end">To</label>
            <input
              id="cal-end"
              class="input num"
              type="date"
              [value]="end()"
              (change)="setRange({ end: $any($event.target).value })"
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
      <section class="panel panel-flush" aria-labelledby="cal-events-title">
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
        } @else if (shown().error(); as err) {
          <app-error-state
            title="Could not load the calendar"
            [error]="err"
            (retry)="shown().reload()"
          />
        } @else if (!shown().hasValue()) {
          <app-loading-state label="Loading the calendar" [rows]="5" />
        } @else {
          @let v = shown().value()!;
          @if (v.truncated) {
            <p class="callout note" data-tone="warn" role="status">
              Only the first events show. Pick fewer days or fewer tickers to see them all.
            </p>
          }
          @switch (tab()) {
            @case ('earnings') {
              @if (v.earnings.length === 0) {
                <app-empty-state title="No earnings in these days" [message]="emptyMessage()" />
              } @else {
                <app-data-table
                  caption="Earnings reports"
                  [rows]="v.earnings"
                  [columns]="earningsColumns"
                  [rowKey]="earningsKey"
                  [initialSort]="{ key: 'report_date', dir: 'asc' }"
                  [pageSize]="50"
                >
                  <ng-template appCell="ticker" [appCellOf]="v.earnings" let-e>
                    <a class="ticker-link" [routerLink]="['/charts', e.ticker]">{{ e.ticker }}</a>
                  </ng-template>
                </app-data-table>
              }
            }
            @case ('dividends') {
              @if (v.dividends.length === 0) {
                <app-empty-state
                  title="No ex-dividend dates in these days"
                  [message]="emptyMessage()"
                />
              } @else {
                <app-data-table
                  caption="Ex-dividend dates"
                  [rows]="v.dividends"
                  [columns]="dividendColumns"
                  [rowKey]="dividendKey"
                  [initialSort]="{ key: 'ex_date', dir: 'asc' }"
                  [pageSize]="50"
                >
                  <ng-template appCell="ticker" [appCellOf]="v.dividends" let-d>
                    <a class="ticker-link" [routerLink]="['/charts', d.ticker]">{{ d.ticker }}</a>
                  </ng-template>
                </app-data-table>
              }
            }
            @case ('economic') {
              @if (v.economic.length === 0) {
                <app-empty-state
                  title="No economic releases in these days"
                  message="Releases arrive with the morning calendar update. Try more days or other countries."
                />
              } @else {
                <app-data-table
                  caption="Economic releases"
                  [rows]="v.economic"
                  [columns]="economicColumns"
                  [rowKey]="economicKey"
                  [initialSort]="{ key: 'event_time', dir: 'asc' }"
                  [pageSize]="50"
                />
              }
            }
            @case ('filings') {
              @let filings = v.filings ?? [];
              @if (filings.length === 0) {
                <app-empty-state
                  title="No filings in these days"
                  [message]="filingsEmptyMessage()"
                />
              } @else {
                <app-data-table
                  caption="SEC filings"
                  [rows]="filings"
                  [columns]="filingColumns"
                  [rowKey]="filingKey"
                  [initialSort]="{ key: 'accepted_at', dir: 'desc' }"
                  [pageSize]="50"
                >
                  <ng-template appCell="ticker" [appCellOf]="filings" let-f>
                    <a class="ticker-link" [routerLink]="['/charts', f.ticker]">{{ f.ticker }}</a>
                  </ng-template>
                  <ng-template appCell="url" [appCellOf]="filings" let-f>
                    @if (safeUrl(f.url); as href) {
                      <a class="filing-link" [href]="href" target="_blank" rel="noopener noreferrer"
                        >Open on SEC<span class="visually-hidden">
                          : {{ f.form }} of {{ f.ticker }}, opens in a new tab</span
                        ></a
                      >
                    } @else {
                      <span class="muted">No link</span>
                    }
                  </ng-template>
                </app-data-table>
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
    /* From and To share the row. A date input has a wide intrinsic size,
       so the fields and inputs may shrink below it and never push the panel. */
    .dates {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: var(--space-3);
      max-width: 26rem;
      min-width: 0;
    }
    .dates .field,
    .dates .input {
      min-width: 0;
    }
    .dates .input {
      width: 100%;
      padding-inline: var(--space-2);
    }
    .field select {
      max-width: 20rem;
    }
    .hint {
      font-size: var(--text-xs);
      color: var(--color-ink-3);
    }
    .error {
      font-size: var(--text-xs);
      color: var(--color-loss);
    }
    .actions {
      display: flex;
      flex-wrap: wrap;
      gap: var(--space-2);
    }
    /* The events panel is flush for its table: the countries form keeps
       the panel padding. */
    .panel-body.countries {
      max-width: 26rem;
      padding: var(--space-4);
      @include bp.phone {
        padding: var(--space-3);
      }
    }
    /* 44px targets on phones, like every link in a row card. */
    .ticker-link {
      font-weight: var(--weight-semibold);
      display: inline-flex;
      align-items: center;
      min-height: var(--touch-min);
      padding-block: var(--space-2);
      @include bp.from-tablet {
        min-height: 0;
        padding-block: 0;
      }
    }
    .filing-link {
      display: inline-flex;
      align-items: center;
      min-height: var(--touch-min);
      @include bp.from-tablet {
        min-height: 24px;
      }
    }
    .muted {
      color: var(--color-ink-3);
    }
    .note {
      margin: var(--space-3) var(--space-4);
      @include bp.phone {
        margin: var(--space-3);
      }
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

  /** Filings are past events, and follow named companies only. */
  protected readonly filingsEmptyMessage = computed(() =>
    this.scope() === 'all'
      ? 'Filings show for your holdings, a watchlist or the tickers you name.'
      : 'Filings are past events: pick an earlier From date to see what these companies filed.',
  );

  protected readonly earningsColumns: TableColumn<EarningsEvent>[] = [
    { key: 'report_date', label: 'Date', format: 'date' },
    { key: 'ticker', label: 'Ticker', mobile: 'title' },
    { key: 'name', label: 'Company', mobile: 'hide' },
    { key: 'when', label: 'When', value: (e) => timingLabel(e.before_after_market) },
    { key: 'period_end', label: 'Quarter ending', format: 'date', mobile: 'hide' },
    { key: 'eps_estimate', label: 'EPS estimate', format: 'money', currency: (e) => e.currency },
    { key: 'eps_actual', label: 'EPS actual', format: 'money', currency: (e) => e.currency },
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
    { key: 'event_type', label: 'Release', mobile: 'title', value: (e) => economicRelease(e) },
    { key: 'country', label: 'Country' },
    { key: 'importance', label: 'Importance', value: (e) => importanceLabel(e.importance) },
    { key: 'period', label: 'Period', mobile: 'hide' },
    economicFigure('actual', 'Actual'),
    economicFigure('estimate', 'Estimate'),
    { ...economicFigure('previous', 'Previous'), mobile: 'hide' },
  ];

  protected readonly filingColumns: TableColumn<FilingEvent>[] = [
    { key: 'accepted_at', label: 'Filed', format: 'datetime' },
    { key: 'ticker', label: 'Ticker', mobile: 'title' },
    { key: 'name', label: 'Company', mobile: 'hide' },
    { key: 'form', label: 'Form' },
    {
      key: 'items',
      label: 'What it announces',
      value: (f) => filingItemsLabel(f),
      sortable: false,
    },
    { key: 'url', label: 'Document', sortable: false },
  ];

  protected readonly safeUrl = safeUrl;
  protected readonly filingKey = (f: FilingEvent) => `${f.ticker}|${f.accepted_at}|${f.form}`;
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
