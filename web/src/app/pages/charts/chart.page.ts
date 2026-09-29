import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  computed,
  inject,
  input,
  linkedSignal,
  resource,
  signal,
} from '@angular/core';
import { Router, RouterLink } from '@angular/router';

import { ChartsService } from '../../api/charts.service';
import type { ChartFillView, ChartSignalView } from '../../api/models';
import { SearchService } from '../../api/search.service';
import { SessionService } from '../../core/auth/session.service';
import { PortfolioContextService } from '../../core/portfolio/portfolio-context.service';
import { WatchlistContextService } from '../../core/watchlists/watchlist-context.service';
import { PriceChart } from '../../shared/chart/price-chart';
import { TimeSeriesChart } from '../../shared/chart/time-series-chart';
import { DataTable, TableCell, type TableColumn } from '../../shared/ui/data-table/data-table';
import { PageHeader } from '../../shared/ui/page-header';
import { strategyDisplayName } from '../../shared/strategy-names';
import { SideTag } from '../../shared/ui/side-tag';
import { NoBook } from '../../shared/ui/no-book';
import { EmptyState, ErrorState, LoadingState } from '../../shared/ui/states';
import { WatchlistFilter } from '../../shared/ui/watchlist-filter';
import {
  AVERAGES,
  type ChartOptions,
  RANGES,
  type RangeId,
  WARMUP,
  chartData,
  chartSummary,
} from './chart-data';
import {
  MAX_COMPARED,
  SHARPE_WINDOWS,
  type SharpeWindow,
  addTickers,
  compareRows,
  compareSeries,
  compareSummary,
  parseTickers,
  performanceSeries,
  performanceSummary,
} from './compare-data';

const SEARCH_DEBOUNCE_MS = 250;

const SIGNAL_WORDS: Record<string, string> = {
  entry: 'Entry',
  exit: 'Exit',
  increase: 'Add',
  decrease: 'Trim',
  risk: 'Risk',
};

/**
 * A price chart per ticker: daily candles and volume, moving averages, your
 * fills marked B and S, and the strategies' signals as dots, with the same
 * fills and signals listed below for screen readers and phones. Below the
 * candles: other tickers compared on one scale (rebased to 100, kept in
 * `?vs=`), and the ticker's rolling Sharpe with its drawdown.
 */
@Component({
  selector: 'app-chart-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    RouterLink,
    PageHeader,
    PriceChart,
    TimeSeriesChart,
    DataTable,
    TableCell,
    SideTag,
    WatchlistFilter,
    LoadingState,
    EmptyState,
    ErrorState,
    NoBook,
  ],
  templateUrl: './chart.page.html',
  styleUrl: './chart.page.scss',
})
export class ChartPage {
  /** Route param (`/charts/:ticker`); absent on `/charts`. */
  readonly ticker = input<string>();
  /** Query param `?vs=MSFT.US,SPY.US`: tickers compared with this one. */
  readonly vs = input<string>();

  private readonly api = inject(ChartsService);
  private readonly search = inject(SearchService);
  private readonly router = inject(Router);
  private readonly portfolioCtx = inject(PortfolioContextService);
  protected readonly watch = inject(WatchlistContextService);
  protected readonly session = inject(SessionService);
  /** No portfolio at all: the fills panel offers to open one (UX-13). */
  protected readonly noBook = this.portfolioCtx.noBook;

  protected readonly ranges = RANGES;
  protected readonly averages = AVERAGES;

  protected readonly symbol = computed(() => (this.ticker() ?? '').trim().toUpperCase());
  protected readonly range = signal<RangeId>('1y');
  protected readonly shownAverages = signal<readonly number[]>([50, 200]);
  protected readonly showFills = signal(true);
  protected readonly showSignals = signal(true);
  protected readonly showVolume = signal(true);

  private readonly bars = computed(() => RANGES.find((r) => r.id === this.range())?.bars ?? 252);

  // ---- compare and performance ---------------------------------------------------
  protected readonly maxCompared = MAX_COMPARED;
  protected readonly sharpeWindows = SHARPE_WINDOWS;
  protected readonly sharpeWindow = signal<SharpeWindow>(63);
  /** Tickers on the compare chart next to this one. */
  protected readonly compared = linkedSignal(() =>
    parseTickers(this.vs())
      .filter((t) => t !== this.symbol())
      .slice(0, MAX_COMPARED),
  );
  protected readonly compareText = signal('');

  protected readonly comparison = resource({
    params: () =>
      this.symbol()
        ? {
            tickers: [this.symbol(), ...this.compared()],
            limit: Math.min(5_000, this.bars()),
            window: this.sharpeWindow(),
          }
        : undefined,
    loader: ({ params }) =>
      this.api.compare(params.tickers, { limit: params.limit, window: params.window }),
  });
  protected readonly compareLines = computed(() =>
    this.comparison.hasValue() ? compareSeries(this.comparison.value()) : [],
  );
  protected readonly compareCaption = computed(() =>
    this.comparison.hasValue() ? compareSummary(this.comparison.value()) : null,
  );
  protected readonly compareTable = computed(() =>
    this.comparison.hasValue() ? compareRows(this.comparison.value()) : [],
  );
  /** The chart's own ticker in the comparison (absent without prices). */
  private readonly own = computed(() =>
    this.comparison.hasValue()
      ? (this.comparison.value().series.find((s) => s.ticker === this.symbol()) ?? null)
      : null,
  );
  protected readonly performance = computed(() => {
    const own = this.own();
    return own ? performanceSeries(own, this.comparison.value()!.window) : [];
  });
  protected readonly performanceText = computed(() => {
    const own = this.own();
    return own ? performanceSummary(own, this.comparison.value()!.window) : null;
  });

  protected readonly chart = resource({
    params: () =>
      this.symbol()
        ? {
            ticker: this.symbol(),
            limit: Math.min(5_000, this.bars() + WARMUP),
            portfolio: this.portfolioCtx.selectedId(),
          }
        : undefined,
    loader: ({ params }) => this.api.chart(params.ticker, { limit: params.limit }),
  });

  protected readonly options = computed<ChartOptions>(() => ({
    bars: this.bars(),
    averages: this.shownAverages(),
    fills: this.showFills(),
    signals: this.showSignals(),
    volume: this.showVolume(),
  }));
  protected readonly data = computed(() =>
    this.chart.hasValue() ? chartData(this.chart.value(), this.options()) : null,
  );
  protected readonly summary = computed(() => {
    const d = this.data();
    return d ? chartSummary(this.symbol(), d) : null;
  });
  protected readonly fills = computed<ChartFillView[]>(() =>
    this.chart.hasValue() ? [...this.chart.value().fills].reverse() : [],
  );
  protected readonly signals = computed<ChartSignalView[]>(() =>
    this.chart.hasValue() ? [...this.chart.value().signals].reverse() : [],
  );

  /** Tickers of the picked watchlist, to jump between charts. */
  protected readonly listTickers = computed(() => this.watch.selected()?.tickers ?? []);

  // ---- ticker search ------------------------------------------------------------
  protected readonly query = linkedSignal(() => this.symbol());
  protected readonly suggestions = signal<string[]>([]);
  private timer: ReturnType<typeof setTimeout> | undefined;

  protected readonly fillColumns: TableColumn<ChartFillView>[] = [
    { key: 'filled_at', label: 'Date', format: 'date', mobile: 'title' },
    { key: 'side', label: 'Side', sortable: false },
    { key: 'quantity', label: 'Qty', value: (f) => Math.abs(f.quantity), format: 'number' },
    { key: 'price', label: 'Price', format: 'money' },
    {
      key: 'strategy_id',
      label: 'Strategy',
      value: (f) => (f.strategy_id ? strategyDisplayName(f.strategy_id) : ''),
      mobile: 'hide',
    },
  ];
  protected readonly signalColumns: TableColumn<ChartSignalView>[] = [
    { key: 'as_of', label: 'Date', format: 'date', mobile: 'title' },
    { key: 'kind', label: 'Signal', value: (s) => SIGNAL_WORDS[s.kind] ?? s.kind },
    { key: 'strategy_id', label: 'Strategy', value: (s) => strategyDisplayName(s.strategy_id) },
    { key: 'reason', label: 'Why', value: (s) => s.reason ?? '', sortable: false },
  ];
  protected readonly fillKey = (f: ChartFillView) => `${f.order_client_id}-${f.filled_at}`;
  protected readonly signalKey = (s: ChartSignalView) => `${s.as_of}-${s.strategy_id}-${s.kind}`;

  /** Numbers each query; only the latest one's answer may show. */
  private searchSeq = 0;

  constructor() {
    void this.portfolioCtx.load();
    inject(DestroyRef).onDestroy(() => clearTimeout(this.timer));
  }

  protected onQuery(text: string): void {
    this.query.set(text);
    clearTimeout(this.timer);
    // Each keystroke outdates any search still on its way.
    const mine = ++this.searchSeq;
    const q = text.trim();
    if (q.length < 2) {
      this.suggestions.set([]);
      return;
    }
    this.timer = setTimeout(async () => {
      try {
        const page = await this.search.instruments(q, 8);
        if (mine === this.searchSeq) this.suggestions.set(page.items.map((i) => i.id));
      } catch {
        if (mine === this.searchSeq) this.suggestions.set([]);
      }
    }, SEARCH_DEBOUNCE_MS);
  }

  protected open(event: Event): void {
    event.preventDefault();
    const t = this.query().trim().toUpperCase();
    if (t) void this.router.navigate(['/charts', t]);
  }

  protected addCompare(event: Event): void {
    event.preventDefault();
    const next = addTickers(this.symbol(), this.compared(), parseTickers(this.compareText()));
    this.compareText.set('');
    this.setCompared(next);
  }

  protected removeCompare(ticker: string): void {
    this.setCompared(this.compared().filter((t) => t !== ticker));
  }

  /** Keep the compared tickers in the address, so a link reopens the same chart. */
  private setCompared(list: string[]): void {
    this.compared.set(list);
    void this.router.navigate([], {
      queryParams: { vs: list.length ? list.join(',') : null },
      queryParamsHandling: 'merge',
      replaceUrl: true,
    });
  }

  protected toggleAverage(length: number): void {
    this.shownAverages.update((list) =>
      list.includes(length) ? list.filter((l) => l !== length) : [...list, length],
    );
  }
}
