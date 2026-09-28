import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  linkedSignal,
  resource,
  signal,
} from '@angular/core';
import { RouterLink } from '@angular/router';

import { InsightsService } from '../../api/insights.service';
import { LookThroughPanel } from './look-through';
import type {
  AllocationSlice,
  HoldingAgreement,
  Opinion,
  PeriodPnl,
  SnapshotView,
} from '../../api/models';
import { PortfolioService } from '../../api/portfolio.service';
import { SessionService } from '../../core/auth/session.service';
import {
  formatDate,
  formatDateTime,
  formatMoney,
  formatNumber,
  formatPercent,
} from '../../core/format/format';
import { dayChangeFrom, dayChangeLine } from '../../core/format/day-change';
import { PortfolioContextService } from '../../core/portfolio/portfolio-context.service';
import { UpdatedAgo, autoRefresh } from '../../shared/auto-refresh';
import { baseCurrencyLine } from '../../shared/base-currency';
import { strategyDisplayName } from '../../shared/strategy-names';
import { DataTable, TableCell, type TableColumn } from '../../shared/ui/data-table/data-table';
import { keepLatest } from '../../shared/ui/data-table/keep-latest';
import { HelpTip } from '../../shared/ui/help-tip';
import { MonthlyReturns } from '../../shared/ui/monthly-returns';
import { ExportButton } from '../../shared/ui/export-button';
import { PageHeader } from '../../shared/ui/page-header';
import { Segmented, type SegmentOption } from '../../shared/ui/segmented';
import { StatTile } from '../../shared/ui/stat-tile';
import { NoBook, bookState } from '../../shared/ui/no-book';
import { EmptyState, ErrorState, LoadingState } from '../../shared/ui/states';
import { InsightsNav } from './insights-nav';

const HISTORY_PAGE = 20;

export type AllocationDimension = 'asset_class' | 'sector' | 'currency' | 'ticker';
export const DIMENSIONS: readonly SegmentOption<AllocationDimension>[] = [
  { value: 'asset_class', label: 'Asset class' },
  { value: 'sector', label: 'Sector' },
  { value: 'currency', label: 'Currency' },
  { value: 'ticker', label: 'Holding' },
];

export const PERIOD_LABELS: Record<PeriodPnl['period'], string> = {
  '1d': 'Day',
  '1w': 'Week',
  '1m': 'Month',
  '3m': 'Three months',
  ytd: 'Year to date',
  '1y': 'Year',
  inception: 'Since the start',
};

/** A strategy's stance on a holding, as words and a shape (never colour alone). */
export const STANCE: Record<Opinion['stance'], { text: string; mark: string }> = {
  agree: { text: 'agrees', mark: '✓' },
  disagree: { text: 'disagrees', mark: '✗' },
  no_view: { text: 'has no view', mark: '–' },
  not_applicable: { text: 'does not trade this', mark: '–' },
  error: { text: 'could not score it', mark: '!' },
};

/** Asset classes as words people read ("Equity", not "equity"). */
export const ASSET_CLASS_LABELS: Record<string, string> = {
  cash: 'Cash',
  equity: 'Equity',
  crypto: 'Crypto',
  commodity: 'Commodity',
  bond: 'Bond',
};

/** "1 agrees, 2 disagree". */
export function agreementLine(h: HoldingAgreement): string {
  const agree = `${h.agree} ${h.agree === 1 ? 'agrees' : 'agree'}`;
  const disagree = `${h.disagree} ${h.disagree === 1 ? 'disagrees' : 'disagree'}`;
  return `${agree}, ${disagree}`;
}

/**
 * Insights on the picked portfolio (a synced broker account too): where the
 * money sits, exposure and beta, returns over periods, risk, which active
 * strategies agree with each holding, and the snapshot history. Admins also
 * get totals across every book, never holdings.
 */
@Component({
  selector: 'app-insights-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    RouterLink,
    PageHeader,
    ExportButton,
    InsightsNav,
    Segmented,
    StatTile,
    DataTable,
    TableCell,
    HelpTip,
    MonthlyReturns,
    UpdatedAgo,
    LoadingState,
    EmptyState,
    ErrorState,
    NoBook,
    LookThroughPanel,
  ],
  templateUrl: './insights.page.html',
  styleUrl: './insights.page.scss',
})
export class InsightsPage {
  private readonly insightsApi = inject(InsightsService);
  private readonly portfolioApi = inject(PortfolioService);
  private readonly portfolioCtx = inject(PortfolioContextService);
  protected readonly session = inject(SessionService);

  /** Real money: the headline figure turns brass. */
  protected readonly live = this.portfolioCtx.live;
  protected readonly canSeeTotals = computed(() => this.session.can('portfolio.totals'));

  /** With no portfolio at all (UX-13) nothing asks for one. */
  protected readonly book = computed(() => bookState(this.portfolioCtx));
  private readonly bookParams = computed(() =>
    this.book() === 'ready' ? { portfolio: this.portfolioCtx.selectedId() } : undefined,
  );
  /** The portfolio the look-through panel reads, once a book is ready. */
  protected readonly lookThroughFor = computed(() =>
    this.book() === 'ready' ? (this.portfolioCtx.selectedId() ?? 'default') : null,
  );
  protected readonly insights = resource({
    params: () => this.bookParams(),
    loader: () => this.insightsApi.get(),
  });
  protected readonly agreement = resource({
    params: () => this.bookParams(),
    loader: () => this.insightsApi.agreement(),
  });
  protected readonly totals = resource({
    params: () => (this.canSeeTotals() ? {} : undefined),
    loader: () => this.insightsApi.totals(),
  });

  protected readonly historyOffset = linkedSignal({
    source: () => this.portfolioCtx.selectedId(),
    computation: () => 0,
  });
  protected readonly history = resource({
    params: () =>
      this.book() === 'ready'
        ? {
            portfolio: this.portfolioCtx.selectedId(),
            limit: HISTORY_PAGE,
            offset: this.historyOffset(),
          }
        : undefined,
    loader: ({ params }) =>
      this.portfolioApi.snapshots({ limit: params.limit, offset: params.offset }),
  });
  protected readonly historyPage = keepLatest(this.history);
  protected readonly historyPageSize = HISTORY_PAGE;

  protected readonly auto = autoRefresh(() => [this.insights, this.agreement, this.history]);

  protected readonly dimensions = DIMENSIONS;
  protected readonly dimension = signal<AllocationDimension>('asset_class');
  protected readonly slices = computed<AllocationSlice[]>(() =>
    this.insights.hasValue() ? this.insights.value().allocation[this.dimension()] : [],
  );

  protected readonly currency = computed(() =>
    this.insights.hasValue() ? this.insights.value().currency : null,
  );
  protected money(value: number | null | undefined): string {
    return formatMoney(value, { currency: this.currency() });
  }
  protected readonly moneyFormat = (value: number) => this.money(value);
  /** A change in money always carries its sign: "+$120.00" (UX-58). */
  protected signedMoney(value: number | null | undefined): string {
    return formatMoney(value, { signed: true, currency: this.currency() });
  }
  protected readonly pct = (value: number | null | undefined, signed = false) =>
    formatPercent(value, { digits: 1, signed });
  protected readonly num = (value: number | null | undefined) => formatNumber(value, { digits: 2 });

  /** The header stays the same while the page loads, so nothing below it jumps. */
  protected readonly description = 'Where your money sits, how it did and which strategies agree.';

  /** Where the figures come from, under the tiles once they load. */
  protected readonly asOf = computed(() => {
    if (!this.insights.hasValue()) return '';
    const i = this.insights.value();
    const from = i.source === 'sync' ? 'the last broker sync' : 'the last trading run';
    return i.taken_at
      ? `From ${from} on ${formatDateTime(i.taken_at)}, valued at the latest prices.`
      : 'No snapshot yet. The first trading run or broker sync fills this page.';
  });

  protected readonly dayChange = computed(() => {
    if (!this.insights.hasValue()) return null;
    const view = this.insights.value();
    const period = view.pnl.find((p) => p.period === '1d');
    // The API's one headline change (`day_change`), the same as Today and Dashboard (M2).
    const day =
      dayChangeFrom(view.day_change) ??
      (period ? { change: period.change, pct: period.change_pct, day: period.end_day } : null);
    if (!day || day.change == null) return null;
    return dayChangeLine(day.change, day.pct, day.day, this.currency());
  });

  protected readonly betaDetail = computed(() => {
    if (!this.insights.hasValue()) return null;
    const e = this.insights.value().exposure;
    if (e.beta == null) return 'No beta for these holdings yet';
    return `Against ${e.benchmark ?? 'the benchmark'}, ${this.pct(e.beta_coverage, false)} of holdings covered`;
  });

  /** Net exposure under the gross figure, said in words. */
  protected readonly netDetail = computed(() => {
    if (!this.insights.hasValue()) return null;
    return `Net ${this.pct(this.insights.value().exposure.net)}, long minus short`;
  });

  /** At least one month with a measured return for the heatmap. */
  protected readonly hasMonths = computed(
    () =>
      this.insights.hasValue() &&
      (this.insights.value().monthly_returns ?? []).some((m) => m.value !== null),
  );

  protected readonly periodLabel = (p: PeriodPnl) => PERIOD_LABELS[p.period];
  /** The time-weighted return of a period: deposits and withdrawals left out. */
  protected readonly twrText = (p: PeriodPnl) => (p.twr == null ? 'n/a' : this.pct(p.twr, true));
  /** The money-weighted return since the start, per year. */
  protected readonly mwrText = (mwr: number | null | undefined) =>
    mwr == null ? 'n/a' : this.pct(mwr, true);

  /** The value in the portfolio's base currency, when it differs (or why it is missing). */
  protected readonly baseLine = computed(() => {
    if (!this.insights.hasValue()) return null;
    return baseCurrencyLine(this.insights.value(), this.portfolioCtx.current()?.base_currency);
  });
  protected readonly stance = STANCE;
  protected readonly agreementLine = agreementLine;

  protected readonly strategyName = (id: string) => strategyDisplayName(id);
  /** The trading day a snapshot is for; older rows fall back to when it was taken. */
  protected readonly snapshotDay = (s: SnapshotView) => formatDate(s.as_of ?? s.taken_at);

  protected readonly historyColumns: TableColumn<SnapshotView>[] = [
    { key: 'as_of', label: 'Trading day', mobile: 'title', value: (s) => s.as_of ?? s.taken_at },
    { key: 'taken_at', label: 'Saved at', format: 'datetime', mobile: 'hide' },
    { key: 'total_value', label: 'Value', format: 'money', currency: () => this.currency() },
    { key: 'cash', label: 'Cash', format: 'money', currency: () => this.currency() },
    {
      key: 'positions',
      label: 'Positions',
      format: 'number',
      value: (s) => Object.keys(s.positions).length,
    },
  ];
  protected readonly snapshotKey = (s: SnapshotView) => String(s.id);
  protected readonly sliceKey = (s: AllocationSlice) => s.key;
  protected readonly sliceLabel = (s: AllocationSlice) =>
    this.dimension() === 'asset_class' || s.key === 'cash'
      ? (ASSET_CLASS_LABELS[s.key] ?? s.key)
      : s.key;
  protected readonly barWidth = (s: AllocationSlice) =>
    `${Math.max(0, Math.min(1, Math.abs(s.weight ?? 0))) * 100}%`;
}
