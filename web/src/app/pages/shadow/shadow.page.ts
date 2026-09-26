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

import type { ShadowDecisionView, ShadowPnlSummary } from '../../api/models';
import { PortfolioService } from '../../api/portfolio.service';
import { ShadowService } from '../../api/shadow.service';
import { formatPercent, toneClass } from '../../core/format/format';
import type { ChartColor, ChartSeries } from '../../shared/chart/chart-engine';
import { TimeSeriesChart } from '../../shared/chart/time-series-chart';
import { DataTable, TableCell, type TableColumn } from '../../shared/ui/data-table/data-table';
import { PageHeader } from '../../shared/ui/page-header';
import { StatTile } from '../../shared/ui/stat-tile';
import { EmptyState, ErrorState, LoadingState } from '../../shared/ui/states';
import { StatusPill } from '../../shared/ui/status-pill';
import { type ShadowComparison, compareToReal, comparisonSeries } from './shadow-compare';

const DECISIONS_PAGE = 50;
/** Shadow lines cycle through these; brass is kept for the real portfolio. */
const SHADOW_COLORS: ChartColor[] = ['primary', 'muted', 'gain', 'loss'];
const ALL = '';

/** A summary row joined with its comparison against the real portfolio. */
export interface ShadowRow extends ShadowPnlSummary {
  comparison: ShadowComparison | null;
}

/**
 * Shadow strategies trade on paper next to the real portfolio. This page puts
 * each one against the real portfolio on one rebased chart, lists how far
 * ahead or behind it is, and links each to its go-live checks.
 */
@Component({
  selector: 'app-shadow-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    RouterLink,
    PageHeader,
    StatTile,
    StatusPill,
    DataTable,
    TableCell,
    TimeSeriesChart,
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
  protected readonly real = resource({ loader: () => this.portfolioApi.pnl() });

  private readonly strategyIds = computed(() =>
    this.summaries.hasValue() ? this.summaries.value().items.map((s) => s.strategy_id) : undefined,
  );
  /** Each shadow strategy's daily series; waits for the summaries. */
  protected readonly series = resource({
    params: () => (this.strategyIds() ? { ids: this.strategyIds()! } : undefined),
    loader: async ({ params }) => {
      const all = await Promise.all(params.ids.map((id) => this.shadowApi.pnl(id)));
      return params.ids.map((id, i) => ({ id, rows: all[i].rows }));
    },
  });

  protected readonly refreshing = computed(
    () => this.summaries.isLoading() || this.real.isLoading() || this.series.isLoading(),
  );

  private readonly realRows = computed(() => (this.real.hasValue() ? this.real.value().rows : []));
  private readonly shadowSeries = computed(() =>
    this.series.hasValue() ? this.series.value() : [],
  );

  protected readonly rows = computed<ShadowRow[]>(() => {
    if (!this.summaries.hasValue()) return [];
    const byId = new Map(this.shadowSeries().map((s) => [s.id, s.rows]));
    const real = this.realRows();
    return this.summaries.value().items.map((s) => {
      const rows = byId.get(s.strategy_id);
      return {
        ...s,
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
    return c ? `${formatPercent(c.excess, { signed: true })} vs real` : null;
  });
  protected readonly leaderTone = computed(() => toneClass(this.leader()?.comparison?.excess));

  // Chart -------------------------------------------------------------------
  /** '' shows every shadow strategy; an id shows one, with its drawdown below. */
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
    const series: ChartSeries[] = [
      {
        id: 'real',
        label: 'Real portfolio',
        kind: 'line',
        color: 'brass',
        format: 'number',
        points: input.real,
      },
      ...input.shadows.map<ChartSeries>((s) => ({
        id: `shadow:${s.id}`,
        label: s.id,
        kind: 'line',
        color: SHADOW_COLORS[Math.max(0, ids.indexOf(s.id)) % SHADOW_COLORS.length],
        format: 'number',
        points: s.points,
      })),
    ];
    const focus = this.focus();
    if (focus !== ALL) {
      const rows = this.shadowSeries().find((s) => s.id === focus)?.rows ?? [];
      series.push({
        id: 'drawdown',
        label: `${focus} drawdown`,
        kind: 'area',
        color: 'loss',
        pane: 1,
        format: 'percent',
        points: rows
          .filter((r) => r.day >= input.baseDay!)
          .map((r) => ({ time: r.day, value: r.drawdown })),
      });
    }
    return series;
  });

  protected readonly chartSummary = computed(() => {
    const input = this.chartInput();
    if (!input.baseDay) return null;
    const end = (points: { value: number }[]) => points.at(-1)?.value.toFixed(1) ?? '–';
    const parts = input.shadows.map((s) => `${s.id} ${end(s.points)}`);
    return (
      `Values rebased to 100 on ${input.baseDay}. Real portfolio ${end(input.real)}; ` +
      `${parts.join(', ')}.`
    );
  });

  protected readonly chartDays = computed(() => this.chartInput().real.length);
  protected readonly baseDay = computed(() => this.chartInput().baseDay);

  // Summary table -----------------------------------------------------------
  protected readonly summaryColumns: TableColumn<ShadowRow>[] = [
    { key: 'strategy_id', label: 'Strategy', mobile: 'title' },
    {
      key: 'days',
      label: 'Days',
      format: 'number',
      value: (r) => r.comparison?.daysElapsed ?? r.days,
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
      label: 'Real, same days',
      format: 'signedPercent',
      tone: true,
      mobile: 'hide',
      value: (r) => r.comparison?.realReturn ?? null,
    },
    {
      key: 'excess',
      label: 'Vs real',
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
    { key: 'go_live', label: 'Go-live', sortable: false, align: 'end' },
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
  protected readonly decisionsPage = DECISIONS_PAGE;

  protected readonly decisionColumns: TableColumn<ShadowDecisionView>[] = [
    { key: 'ticker', label: 'Ticker', mobile: 'title' },
    { key: 'strategy_id', label: 'Strategy' },
    { key: 'side', label: 'Side' },
    { key: 'quantity', label: 'Qty', format: 'number' },
    { key: 'price', label: 'Price', format: 'money' },
    { key: 'status', label: 'Status' },
    { key: 'as_of', label: 'As of', format: 'date' },
    { key: 'tick_id', label: 'Tick', mobile: 'hide' },
  ];
  protected readonly decisionRowKey = (d: ShadowDecisionView) => String(d.id);

  protected setTicker(raw: string): void {
    this.decisionTicker.set(raw.trim().toUpperCase());
  }

  protected refresh(): void {
    // Reloading the summaries refetches every series (their params follow the ids).
    this.summaries.reload();
    this.real.reload();
    this.decisions.reload();
  }
}
