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
import { DataTable, TableCell, type TableColumn } from '../../shared/ui/data-table/data-table';
import { PageHeader } from '../../shared/ui/page-header';
import { SideTag } from '../../shared/ui/side-tag';
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
 * fills and signals listed below for screen readers and phones.
 */
@Component({
  selector: 'app-chart-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    RouterLink,
    PageHeader,
    PriceChart,
    DataTable,
    TableCell,
    SideTag,
    WatchlistFilter,
    LoadingState,
    EmptyState,
    ErrorState,
  ],
  templateUrl: './chart.page.html',
  styleUrl: './chart.page.scss',
})
export class ChartPage {
  /** Route param (`/charts/:ticker`); absent on `/charts`. */
  readonly ticker = input<string>();

  private readonly api = inject(ChartsService);
  private readonly search = inject(SearchService);
  private readonly router = inject(Router);
  private readonly portfolioCtx = inject(PortfolioContextService);
  protected readonly watch = inject(WatchlistContextService);
  protected readonly session = inject(SessionService);

  protected readonly ranges = RANGES;
  protected readonly averages = AVERAGES;

  protected readonly symbol = computed(() => (this.ticker() ?? '').trim().toUpperCase());
  protected readonly range = signal<RangeId>('1y');
  protected readonly shownAverages = signal<readonly number[]>([50, 200]);
  protected readonly showFills = signal(true);
  protected readonly showSignals = signal(true);
  protected readonly showVolume = signal(true);

  private readonly bars = computed(() => RANGES.find((r) => r.id === this.range())?.bars ?? 252);

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
    { key: 'strategy_id', label: 'Strategy', value: (f) => f.strategy_id ?? '', mobile: 'hide' },
  ];
  protected readonly signalColumns: TableColumn<ChartSignalView>[] = [
    { key: 'as_of', label: 'Date', format: 'date', mobile: 'title' },
    { key: 'kind', label: 'Signal', value: (s) => SIGNAL_WORDS[s.kind] ?? s.kind },
    { key: 'strategy_id', label: 'Strategy' },
    { key: 'reason', label: 'Why', value: (s) => s.reason ?? '', sortable: false },
  ];
  protected readonly fillKey = (f: ChartFillView) => `${f.order_client_id}-${f.filled_at}`;
  protected readonly signalKey = (s: ChartSignalView) => `${s.as_of}-${s.strategy_id}-${s.kind}`;

  constructor() {
    void this.portfolioCtx.load();
    inject(DestroyRef).onDestroy(() => clearTimeout(this.timer));
  }

  protected onQuery(text: string): void {
    this.query.set(text);
    clearTimeout(this.timer);
    const q = text.trim();
    if (q.length < 2) {
      this.suggestions.set([]);
      return;
    }
    this.timer = setTimeout(async () => {
      try {
        const page = await this.search.instruments(q, 8);
        this.suggestions.set(page.items.map((i) => i.id));
      } catch {
        this.suggestions.set([]);
      }
    }, SEARCH_DEBOUNCE_MS);
  }

  protected open(event: Event): void {
    event.preventDefault();
    const t = this.query().trim().toUpperCase();
    if (t) void this.router.navigate(['/charts', t]);
  }

  protected toggleAverage(length: number): void {
    this.shownAverages.update((list) =>
      list.includes(length) ? list.filter((l) => l !== length) : [...list, length],
    );
  }
}
