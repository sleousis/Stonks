import { ChangeDetectionStrategy, Component, computed, inject, resource } from '@angular/core';
import { RouterLink } from '@angular/router';

import { HealthService } from '../../api/health.service';
import type { PositionView, TickRun } from '../../api/models';
import { PortfolioService } from '../../api/portfolio.service';
import { StrategiesService } from '../../api/strategies.service';
import { TicksService } from '../../api/ticks.service';
import {
  formatDateTime,
  formatDuration,
  formatMoney,
  formatPercent,
  toneClass,
} from '../../core/format/format';
import type { ChartSeries } from '../../shared/chart/chart-engine';
import { TimeSeriesChart } from '../../shared/chart/time-series-chart';
import { DataTable, TableCell, type TableColumn } from '../../shared/ui/data-table/data-table';
import { PageHeader } from '../../shared/ui/page-header';
import { StatTile } from '../../shared/ui/stat-tile';
import { EmptyState, ErrorState, LoadingState } from '../../shared/ui/states';
import { StatusPill } from '../../shared/ui/status-pill';

const RECENT_TICKS = 8;

/**
 * Reference page: read-only overview built from GET routes. Each panel owns
 * one `resource()` and renders loading / error / empty / data on its own, so
 * one failing route never blanks the whole page.
 */
@Component({
  selector: 'app-dashboard-page',
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
  templateUrl: './dashboard.page.html',
  styleUrl: './dashboard.page.scss',
})
export class DashboardPage {
  private readonly portfolioApi = inject(PortfolioService);
  private readonly ticksApi = inject(TicksService);
  private readonly healthApi = inject(HealthService);
  private readonly strategiesApi = inject(StrategiesService);

  protected readonly portfolio = resource({ loader: () => this.portfolioApi.get() });
  protected readonly pnl = resource({ loader: () => this.portfolioApi.pnl() });
  protected readonly ticks = resource({
    loader: () => this.ticksApi.list({ limit: RECENT_TICKS }),
  });
  protected readonly health = resource({ loader: () => this.healthApi.report() });
  protected readonly version = resource({ loader: () => this.healthApi.ping() });
  protected readonly strategyCounts = resource({
    loader: async () => {
      const [active, shadow] = await Promise.all([
        this.strategiesApi.count('active'),
        this.strategiesApi.count('shadow'),
      ]);
      return { active, shadow };
    },
  });

  protected readonly refreshing = computed(
    () =>
      this.portfolio.isLoading() ||
      this.pnl.isLoading() ||
      this.ticks.isLoading() ||
      this.health.isLoading() ||
      this.strategyCounts.isLoading(),
  );

  // Summary tiles -----------------------------------------------------------
  protected readonly asOf = computed(() => {
    if (!this.portfolio.hasValue()) return 'Portfolio, performance and system status.';
    const takenAt = this.portfolio.value().taken_at;
    return takenAt
      ? `Positions from the snapshot of ${formatDateTime(takenAt)}, valued at the latest prices.`
      : 'No snapshot yet: the first tick seeds the portfolio.';
  });

  protected readonly rows = computed(() => (this.pnl.hasValue() ? this.pnl.value().rows : []));
  private readonly latest = computed(() => this.rows().at(-1) ?? null);

  protected readonly dayChange = computed(() => {
    const row = this.latest();
    if (!row || row.daily_change == null) return null;
    return `${formatMoney(row.daily_change, { signed: true })} (${formatPercent(row.daily_return, { signed: true })}) on ${row.day}`;
  });
  protected readonly dayTone = computed(() => toneClass(this.latest()?.daily_change));

  protected readonly cashShare = computed(() => {
    if (!this.portfolio.hasValue()) return null;
    const p = this.portfolio.value();
    return p.total_value
      ? `${formatPercent(p.cash / p.total_value, { digits: 1 })} of value`
      : null;
  });

  protected readonly positionsDetail = computed(() => {
    if (!this.portfolio.hasValue()) return null;
    const n = this.portfolio.value().positions.length;
    return n === 1 ? '1 position' : `${n} positions`;
  });

  protected readonly drawdownNow = computed(() => this.latest()?.drawdown ?? null);
  protected readonly worstDrawdown = computed(() => {
    const rows = this.rows();
    return rows.length ? Math.min(...rows.map((r) => r.drawdown)) : null;
  });

  protected readonly money = formatMoney;
  protected readonly percent = formatPercent;

  // Chart -------------------------------------------------------------------
  protected readonly chartSeries = computed<ChartSeries[]>(() => {
    const rows = this.rows();
    return [
      {
        id: 'value',
        label: 'Value',
        kind: 'line',
        color: 'brass',
        format: 'money',
        points: rows.map((r) => ({ time: r.day, value: r.total_value })),
      },
      {
        id: 'drawdown',
        label: 'Drawdown',
        kind: 'area',
        color: 'loss',
        pane: 1,
        format: 'percent',
        points: rows.map((r) => ({ time: r.day, value: r.drawdown })),
      },
    ];
  });

  protected readonly chartSummary = computed(() => {
    const rows = this.rows();
    const first = rows[0];
    const last = rows.at(-1);
    if (!first || !last) return null;
    return (
      `Portfolio value from ${first.day} to ${last.day}: ${formatMoney(first.total_value)} to ` +
      `${formatMoney(last.total_value)}, cumulative return ${formatPercent(last.cumulative_return, { signed: true })}. ` +
      `Current drawdown ${formatPercent(last.drawdown)}, worst ${formatPercent(this.worstDrawdown())}.`
    );
  });

  // Tables ------------------------------------------------------------------
  protected readonly positionColumns: TableColumn<PositionView>[] = [
    { key: 'ticker', label: 'Ticker', mobile: 'title' },
    { key: 'quantity', label: 'Quantity', format: 'number' },
    { key: 'price', label: 'Price', format: 'money' },
    { key: 'price_date', label: 'Priced', format: 'date', mobile: 'hide' },
    { key: 'market_value', label: 'Value', format: 'money' },
    { key: 'weight', label: 'Weight', format: 'percent' },
  ];
  protected readonly positionKey = (p: PositionView) => p.ticker;

  protected readonly tickColumns: TableColumn<TickRun>[] = [
    { key: 'started_at', label: 'Started', format: 'datetime', mobile: 'title' },
    { key: 'status', label: 'Status' },
    {
      key: 'orders',
      label: 'Orders',
      format: 'number',
      value: (t) => t.summary?.orders_placed ?? null,
    },
    { key: 'fills', label: 'Fills', format: 'number', value: (t) => t.summary?.fills ?? null },
    {
      key: 'winner',
      label: 'Winner',
      value: (t) => t.summary?.winner_strategy_id ?? null,
      mobile: 'hide',
    },
    {
      key: 'duration',
      label: 'Took',
      sortable: false,
      value: (t) => formatDuration(t.started_at, t.finished_at),
      align: 'end',
      mobile: 'hide',
    },
  ];
  protected readonly tickKey = (t: TickRun) => t.id;

  // Health ------------------------------------------------------------------
  protected readonly failingChecks = computed(() =>
    this.health.hasValue() ? this.health.value().checks.filter((c) => !c.ok).length : 0,
  );

  protected refresh(): void {
    this.portfolio.reload();
    this.pnl.reload();
    this.ticks.reload();
    this.health.reload();
    this.version.reload();
    this.strategyCounts.reload();
  }
}
