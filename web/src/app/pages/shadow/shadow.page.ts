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

import type { PnlRowView, ShadowDecisionView, ShadowPnlSummary } from '../../api/models';
import { PortfolioService } from '../../api/portfolio.service';
import { ShadowService } from '../../api/shadow.service';
import { formatDate, formatNumber, formatPercent, toneClass } from '../../core/format/format';
import { CATEGORICAL_LINES, type ChartSeries } from '../../shared/chart/chart-engine';
import { TimeSeriesChart } from '../../shared/chart/time-series-chart';
import { UpdatedAgo, autoRefresh } from '../../shared/auto-refresh';
import { DataTable, TableCell, type TableColumn } from '../../shared/ui/data-table/data-table';
import { keepLatest } from '../../shared/ui/data-table/keep-latest';
import { strategyDisplayName } from '../../shared/strategy-names';
import { ModeStamp } from '../../shared/ui/mode-stamp';
import { SideTag } from '../../shared/ui/side-tag';
import { PageHeader } from '../../shared/ui/page-header';
import { StatTile } from '../../shared/ui/stat-tile';
import { EmptyState, ErrorState, LoadingState } from '../../shared/ui/states';
import { StatusPill } from '../../shared/ui/status-pill';
import { type ShadowComparison, compareToReal, comparisonSeries } from './shadow-compare';
import { PortfolioContextService } from '../../core/portfolio/portfolio-context.service';

const DECISIONS_PAGE = 50;
/**
 * The chart draws at most this many paper strategies, the ones with the best
 * return, each in its own categorical line style (brass stays your portfolio
 * when it trades real money). The table still lists every one.
 */
export const SHADOW_CHART_MAX = CATEGORICAL_LINES.length;
const ALL = '';

/** The strategies worth a line on the chart: best return first, missing returns last. */
export function chartedIds(items: readonly ShadowPnlSummary[], max = SHADOW_CHART_MAX): string[] {
  return [...items]
    .sort((a, b) => (b.cumulative_return ?? -Infinity) - (a.cumulative_return ?? -Infinity))
    .slice(0, max)
    .map((s) => s.strategy_id);
}

function sameIds(a: readonly string[] | undefined, b: readonly string[] | undefined): boolean {
  if (!a || !b) return a === b;
  return a.length === b.length && a.every((id, i) => id === b[i]);
}

/**
 * How far a test book is ahead of or behind your portfolio, in words that
 * never call a trailing strategy "leading" (m5).
 */
export function aheadOrBehind(excess: number | null): string | null {
  if (excess == null) return null;
  if (excess === 0) return 'Level with your portfolio';
  const gap = formatPercent(Math.abs(excess));
  return excess > 0 ? `${gap} ahead of your portfolio` : `${gap} behind your portfolio`;
}

/** A summary row joined with its comparison against your portfolio. */
export interface ShadowRow extends ShadowPnlSummary {
  /** The name to show (UX-27). */
  name: string;
  comparison: ShadowComparison | null;
}

/**
 * The Trial results page (`/paper`): each strategy's test book next to your
 * portfolio. It puts each one against your portfolio on one chart, lists how
 * far ahead or behind it is, and links each to the Review tab of its
 * strategy page. Your portfolio is named "Your portfolio" with its PAPER or
 * LIVE stamp, never "Real portfolio" on paper money (UX-26).
 */
@Component({
  selector: 'app-shadow-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    RouterLink,
    PageHeader,
    StatTile,
    StatusPill,
    ModeStamp,
    DataTable,
    TableCell,
    TimeSeriesChart,
    UpdatedAgo,
    SideTag,
    LoadingState,
    EmptyState,
    ErrorState,
  ],
  templateUrl: './shadow.page.html',
  styleUrl: './shadow.page.scss',
})
export class ShadowPage {
  private readonly shadowApi = inject(ShadowService);
  private readonly portfolioApi = inject(PortfolioService);

  // Data --------------------------------------------------------------------
  protected readonly summaries = resource({
    loader: () => this.shadowApi.pnlSummaries({ limit: 100 }),
  });
  private readonly portfolioCtx = inject(PortfolioContextService);
  /** Your portfolio trades real money: its line is brass and its stamp says LIVE. */
  protected readonly live = this.portfolioCtx.live;
  protected readonly name = strategyDisplayName;
  protected readonly date = formatDate;
  protected readonly real = resource({
    params: () => ({ portfolio: this.portfolioCtx.selectedId() }),
    loader: () => this.portfolioApi.pnl(),
  });

  /** The charted strategies (top by return). Equal lists keep the series from refetching. */
  protected readonly strategyIds = computed(
    () => (this.summaries.hasValue() ? chartedIds(this.summaries.value().items) : undefined),
    { equal: sameIds },
  );
  /**
   * The charted strategies' daily series; waits for the summaries. One
   * request per line, settled independently: a failing series is named in a
   * note and the others still draw.
   */
  protected readonly series = resource({
    params: () => (this.strategyIds() ? { ids: this.strategyIds()! } : undefined),
    loader: async ({ params }) => {
      const settled = await Promise.allSettled(params.ids.map((id) => this.shadowApi.pnl(id)));
      const loaded: { id: string; rows: PnlRowView[] }[] = [];
      const failed: string[] = [];
      settled.forEach((r, i) => {
        if (r.status === 'fulfilled') loaded.push({ id: params.ids[i], rows: r.value.rows });
        else failed.push(params.ids[i]);
      });
      if (!loaded.length && failed.length) throw (settled[0] as PromiseRejectedResult).reason;
      return { loaded, failed };
    },
  });
  /** The last loaded series stay on the chart while a refresh loads. */
  private readonly seriesShown = keepLatest(this.series);
  protected readonly failedSeries = computed(() => this.seriesShown()?.failed ?? []);
  protected readonly failedNames = computed(() =>
    this.failedSeries()
      .map((id) => strategyDisplayName(id))
      .join(', '),
  );
  protected readonly hiddenCount = computed(() =>
    this.summaries.hasValue()
      ? Math.max(0, this.summaries.value().items.length - (this.strategyIds()?.length ?? 0))
      : 0,
  );

  protected readonly refreshing = computed(
    () => this.summaries.isLoading() || this.real.isLoading() || this.series.isLoading(),
  );

  private readonly realRows = computed(() => (this.real.hasValue() ? this.real.value().rows : []));
  private readonly shadowSeries = computed(() => this.seriesShown()?.loaded ?? []);
  protected readonly seriesReady = computed(() => this.seriesShown() !== undefined);

  protected readonly rows = computed<ShadowRow[]>(() => {
    if (!this.summaries.hasValue()) return [];
    const byId = new Map(this.shadowSeries().map((s) => [s.id, s.rows]));
    const real = this.realRows();
    return this.summaries.value().items.map((s) => {
      const rows = byId.get(s.strategy_id);
      return {
        ...s,
        name: strategyDisplayName(s.strategy_id),
        comparison: rows?.length ? compareToReal(s.strategy_id, rows, real) : null,
      };
    });
  });

  // Tiles -------------------------------------------------------------------
  protected readonly leader = computed<ShadowRow | null>(() => {
    const ranked = this.rows()
      .filter((r) => r.comparison?.excess != null)
      .sort((a, b) => b.comparison!.excess! - a.comparison!.excess!);
    return ranked[0] ?? null;
  });
  protected readonly leaderDetail = computed(() => {
    const c = this.leader()?.comparison;
    return c ? aheadOrBehind(c.excess) : null;
  });
  protected readonly leaderTone = computed(() => toneClass(this.leader()?.comparison?.excess));

  // Chart -------------------------------------------------------------------
  /** '' shows every test book; an id shows one, with its drawdown below. */
  protected readonly focus = linkedSignal<string[] | undefined, string>({
    source: () => this.strategyIds(),
    computation: (ids, prev) => (prev && ids?.includes(prev.value) ? prev.value : ALL),
  });

  private readonly chartInput = computed(() => {
    const focus = this.focus();
    const shadows = this.shadowSeries().filter((s) => focus === ALL || s.id === focus);
    return comparisonSeries(this.realRows(), shadows);
  });

  protected readonly chartSeries = computed<ChartSeries[]>(() => {
    const input = this.chartInput();
    if (!input.baseDay) return [];
    const ids = this.strategyIds() ?? [];
    const style = (id: string) =>
      CATEGORICAL_LINES[Math.max(0, ids.indexOf(id)) % CATEGORICAL_LINES.length];
    const series: ChartSeries[] = [
      {
        id: 'real',
        label: 'Your portfolio',
        kind: 'line',
        color: this.portfolioCtx.live() ? 'brass' : 'muted',
        format: 'number',
        points: input.real,
      },
      ...input.shadows.map<ChartSeries>((s) => ({
        id: `shadow:${s.id}`,
        label: strategyDisplayName(s.id),
        kind: 'line',
        color: style(s.id).color,
        dashed: style(s.id).dashed,
        format: 'number',
        points: s.points,
      })),
    ];
    const focus = this.focus();
    if (focus !== ALL) {
      const rows = this.shadowSeries().find((s) => s.id === focus)?.rows ?? [];
      series.push({
        id: 'drawdown',
        label: `${strategyDisplayName(focus)} drawdown`,
        kind: 'area',
        color: 'loss',
        pane: 1,
        format: 'percent',
        points: rows.map((r) => ({ time: r.day, value: r.drawdown })),
      });
    }
    return series;
  });

  protected readonly chartSummary = computed(() => {
    const input = this.chartInput();
    if (!input.baseDay) return null;
    const end = (points: { value: number }[]) =>
      formatNumber(points.at(-1)?.value ?? null, { digits: 1 });
    const parts = input.shadows.map((s) => `${strategyDisplayName(s.id)} ${end(s.points)}`);
    return (
      `Your portfolio starts at 100 on ${formatDate(input.baseDay)} and ends at ` +
      `${end(input.real)}. Each strategy starts on its own first day: ${parts.join(', ')}.`
    );
  });

  protected readonly chartDays = computed(() => this.chartInput().real.length);
  protected readonly baseDay = computed(() => this.chartInput().baseDay);
  protected readonly baseDayText = computed(() => {
    const day = this.baseDay();
    return day ? formatDate(day) : null;
  });

  // Summary table -----------------------------------------------------------
  protected readonly summaryColumns: TableColumn<ShadowRow>[] = [
    { key: 'strategy_id', label: 'Strategy', mobile: 'title', value: (r) => r.name },
    {
      key: 'days',
      label: 'Trial days',
      format: 'number',
      // The count of days on its test book, the same days the chart draws (m5).
      value: (r) => r.days,
    },
    {
      key: 'return',
      label: 'Return',
      format: 'signedPercent',
      tone: true,
      value: (r) => r.comparison?.shadowReturn ?? r.cumulative_return,
    },
    {
      key: 'real',
      label: 'Yours, same days',
      format: 'signedPercent',
      tone: true,
      mobile: 'hide',
      value: (r) => r.comparison?.realReturn ?? null,
    },
    {
      key: 'excess',
      label: 'Vs yours',
      format: 'signedPercent',
      tone: true,
      value: (r) => r.comparison?.excess ?? null,
    },
    {
      key: 'drawdown',
      label: 'Drawdown',
      format: 'percent',
      mobile: 'hide',
      value: (r) => r.comparison?.drawdown ?? null,
    },
    {
      key: 'max_drawdown',
      label: 'Max drawdown',
      format: 'percent',
      value: (r) => r.comparison?.maxDrawdown ?? r.max_drawdown,
    },
    { key: 'total_value', label: 'Value', format: 'money', mobile: 'hide' },
    { key: 'status', label: 'Status', mobile: 'hide' },
    { key: 'go_live', label: 'Review', sortable: false, align: 'end' },
  ];
  protected readonly summaryKey = (r: ShadowRow) => r.strategy_id;

  // Decisions ---------------------------------------------------------------
  protected readonly decisionStrategy = signal('');
  protected readonly decisionTicker = signal('');
  private readonly decisionFilters = computed(() => ({
    strategy_id: this.decisionStrategy() || null,
    ticker: this.decisionTicker() || null,
  }));
  protected readonly decisionKey = computed(() => JSON.stringify(this.decisionFilters()));
  protected readonly decisionOffset = linkedSignal({
    source: this.decisionKey,
    computation: () => 0,
  });
  protected readonly decisions = resource({
    params: () => ({
      ...this.decisionFilters(),
      limit: DECISIONS_PAGE,
      offset: this.decisionOffset(),
    }),
    loader: ({ params }) => this.shadowApi.decisions(params),
  });
  /** The last loaded page stays on screen while the next one loads. */
  protected readonly decisionsPageShown = keepLatest(this.decisions);
  protected readonly decisionsPage = DECISIONS_PAGE;

  protected readonly decisionColumns: TableColumn<ShadowDecisionView>[] = [
    { key: 'ticker', label: 'Ticker', mobile: 'title' },
    { key: 'strategy_id', label: 'Strategy', value: (d) => strategyDisplayName(d.strategy_id) },
    { key: 'side', label: 'Side' },
    { key: 'quantity', label: 'Qty', format: 'number' },
    { key: 'price', label: 'Price', format: 'money' },
    { key: 'status', label: 'Status' },
    { key: 'as_of', label: 'As of', format: 'date' },
    { key: 'tick_id', label: 'Run', sortable: false, mobile: 'hide' },
  ];
  protected readonly decisionRowKey = (d: ShadowDecisionView) => String(d.id);

  protected setTicker(raw: string): void {
    this.decisionTicker.set(raw.trim().toUpperCase());
  }

  protected readonly auto = autoRefresh(() => [
    this.summaries,
    this.real,
    this.series,
    this.decisions,
  ]);

  protected refresh(): void {
    this.auto.refresh();
  }
}
