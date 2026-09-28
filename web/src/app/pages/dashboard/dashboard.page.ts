import { ChangeDetectionStrategy, Component, computed, inject, resource } from '@angular/core';
import { RouterLink } from '@angular/router';

import { HealthService } from '../../api/health.service';
import type { PositionView, TickRun } from '../../api/models';
import { PortfolioService } from '../../api/portfolio.service';
import { StrategiesService } from '../../api/strategies.service';
import { TicksService } from '../../api/ticks.service';
import { formatDateTime, formatMoney, formatPercent, toneClass } from '../../core/format/format';
import { dayChangeFrom, dayChangeLine } from '../../core/format/day-change';
import type { ChartSeries } from '../../shared/chart/chart-engine';
import { TimeSeriesChart } from '../../shared/chart/time-series-chart';
import { UpdatedAgo, autoRefresh } from '../../shared/auto-refresh';
import { checkPill, checkTitle, plainDetail, splitChecks } from '../health/health-state';
import { DataTable, TableCell, type TableColumn } from '../../shared/ui/data-table/data-table';
import { DateTimePipe, DayPipe } from '../../shared/format.pipes';
import { PageHeader } from '../../shared/ui/page-header';
import { StatTile } from '../../shared/ui/stat-tile';
import { NoBook, bookState } from '../../shared/ui/no-book';
import { EmptyState, ErrorState, LoadingState } from '../../shared/ui/states';
import { StatusPill } from '../../shared/ui/status-pill';
import { runWords } from '../../shared/status-words';
import { PortfolioContextService } from '../../core/portfolio/portfolio-context.service';

const RECENT_TICKS = 8;

/**
 * The admin's Dashboard (the menu and the title both say Dashboard): the
 * picked portfolio next to system health and the latest trading runs.
 * Read only, built from GET routes. Each panel owns one `resource()` and
 * renders loading / error / empty / data on its own, so one failing route
 * never blanks the whole page. Everything reloads every minute while the
 * tab is visible, and right after a trading run this tab followed ends.
 * Tables keep to the columns that fit their panel (M10): the full detail
 * lives on Insights and on each trading run's page.
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
    UpdatedAgo,
    DateTimePipe,
    DayPipe,
    LoadingState,
    EmptyState,
    ErrorState,
    NoBook,
  ],
  templateUrl: './dashboard.page.html',
  styleUrl: './dashboard.page.scss',
})
export class DashboardPage {
  private readonly portfolioApi = inject(PortfolioService);
  private readonly ticksApi = inject(TicksService);
  private readonly healthApi = inject(HealthService);
  private readonly strategiesApi = inject(StrategiesService);

  private readonly portfolioCtx = inject(PortfolioContextService);
  /** Real money: the headline figure and the value line turn brass. */
  protected readonly live = this.portfolioCtx.live;

  // The picked portfolio is in the params so a new pick reloads these.
  // With no portfolio at all (UX-13) nothing asks for one.
  protected readonly book = computed(() => bookState(this.portfolioCtx));
  private readonly bookParams = computed(() =>
    this.book() === 'ready' ? { portfolio: this.portfolioCtx.selectedId() } : undefined,
  );
  protected readonly portfolio = resource({
    params: () => this.bookParams(),
    loader: () => this.portfolioApi.get(),
  });
  protected readonly pnl = resource({
    params: () => this.bookParams(),
    loader: () => this.portfolioApi.pnl(),
  });
  protected readonly ticks = resource({
    loader: () => this.ticksApi.list({ limit: RECENT_TICKS }),
  });
  protected readonly health = resource({ loader: () => this.healthApi.report() });
  protected readonly version = resource({ loader: () => this.healthApi.ping() });
  protected readonly strategyCounts = resource({ loader: () => this.strategiesApi.summary() });

  protected readonly auto = autoRefresh(
    () => [this.portfolio, this.pnl, this.ticks, this.health, this.version, this.strategyCounts],
    { triggers: [this.ticksApi.finished] },
  );

  // Summary tiles -----------------------------------------------------------
  protected readonly asOf = computed(() => {
    if (!this.portfolio.hasValue()) return 'Portfolio, performance and system status.';
    const takenAt = this.portfolio.value().taken_at;
    return takenAt
      ? `Positions from the snapshot of ${formatDateTime(takenAt)}, valued at the latest prices.`
      : 'No snapshot yet. The first trading run seeds the portfolio.';
  });

  protected readonly rows = computed(() => (this.pnl.hasValue() ? this.pnl.value().rows : []));
  private readonly latest = computed(() => this.rows().at(-1) ?? null);

  /** The API's one headline change (`day_change`), the same as Today and Insights. */
  private readonly day = computed(() =>
    dayChangeFrom(this.pnl.hasValue() ? this.pnl.value().day_change : null, this.latest()),
  );
  protected readonly dayChange = computed(() => {
    const d = this.day();
    if (!d || d.change == null) return null;
    return dayChangeLine(d.change, d.pct, d.day, this.currency());
  });
  protected readonly dayTone = computed(() => toneClass(this.day()?.change));

  protected readonly cashShare = computed(() => {
    if (!this.portfolio.hasValue()) return null;
    const p = this.portfolio.value();
    return p.total_value
      ? `${formatPercent(p.cash / p.total_value, { digits: 1 })} of value`
      : null;
  });

  protected readonly positionsDetail = computed(() => {
    if (!this.portfolio.hasValue()) return null;
    const p = this.portfolio.value();
    const n = p.positions.length;
    const count = n === 1 ? '1 position' : `${n} positions`;
    if (!n || p.unrealized_pnl == null) return count;
    return `${count}, ${formatMoney(p.unrealized_pnl, { signed: true, currency: p.currency })} unrealized`;
  });
  protected readonly positionsTone = computed(() =>
    this.portfolio.hasValue() ? toneClass(this.portfolio.value().unrealized_pnl) : '',
  );

  protected readonly drawdownNow = computed(() => this.latest()?.drawdown ?? null);
  protected readonly worstDrawdown = computed(() => {
    const rows = this.rows();
    return rows.length ? Math.min(...rows.map((r) => r.drawdown)) : null;
  });

  /** The portfolio's currency from the API (USD until it loads). */
  protected readonly currency = computed(() =>
    this.portfolio.hasValue() ? (this.portfolio.value().currency ?? null) : null,
  );
  protected money(value: number | null | undefined): string {
    return formatMoney(value, { currency: this.currency() });
  }
  /** For the headline count-up. */
  protected readonly moneyFormat = (value: number) => this.money(value);
  protected readonly percent = formatPercent;

  // Chart -------------------------------------------------------------------
  protected readonly chartSeries = computed<ChartSeries[]>(() => {
    const rows = this.rows();
    return [
      {
        id: 'value',
        label: 'Value',
        kind: 'line',
        color: this.live() ? 'brass' : 'primary',
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
      `Portfolio value from ${first.day} to ${last.day}: ${this.money(first.total_value)} to ` +
      `${this.money(last.total_value)}, cumulative return ${formatPercent(last.cumulative_return, { signed: true })}. ` +
      `Current drawdown ${formatPercent(last.drawdown)}, worst ${formatPercent(this.worstDrawdown())}.`
    );
  });

  // Tables ------------------------------------------------------------------
  /**
   * A summary: the columns that fit a half-width panel at desktop width
   * (M10). Cost, price and dates are on Insights.
   */
  protected readonly positionColumns: TableColumn<PositionView>[] = [
    { key: 'ticker', label: 'Ticker', mobile: 'title' },
    { key: 'quantity', label: 'Quantity', format: 'number', mobile: 'hide' },
    { key: 'market_value', label: 'Value', format: 'money', currency: (p) => p.currency },
    {
      key: 'unrealized_pnl',
      label: 'Unrealized P&L',
      format: 'signedMoney',
      tone: true,
      currency: (p) => p.currency,
    },
    { key: 'weight', label: 'Weight', format: 'percent' },
  ];
  protected readonly positionKey = (p: PositionView) => p.ticker;

  /** The trading day each run was for, its outcome and counts; the rest is on its page. */
  protected readonly tickColumns: TableColumn<TickRun>[] = [
    {
      key: 'as_of',
      label: 'For',
      mobile: 'title',
      value: (t) => t.as_of ?? t.started_at,
    },
    { key: 'status', label: 'Status', value: (t) => runWords(t.status).label },
    {
      key: 'orders',
      label: 'Orders',
      format: 'number',
      value: (t) => t.summary?.orders_placed ?? null,
    },
    { key: 'fills', label: 'Fills', format: 'number', value: (t) => t.summary?.fills ?? null },
  ];
  /** Done, Partly done, Failed: the vocabulary's words, not the API's (M5). */
  protected readonly runWords = runWords;
  protected readonly tickKey = (t: TickRun) => t.id;
  protected readonly checkTitle = checkTitle;
  protected readonly checkPill = checkPill;
  protected readonly plainDetail = plainDetail;

  // Health ------------------------------------------------------------------
  protected readonly failingChecks = computed(() =>
    this.health.hasValue() ? this.health.value().checks.filter((c) => !c.ok).length : 0,
  );
  protected readonly checkCount = computed(() =>
    this.health.hasValue() ? this.health.value().checks.length : 0,
  );
  /** Per-ticker freshness folds into one line; the Health page lists each ticker. */
  private readonly split = computed(() =>
    this.health.hasValue() ? splitChecks(this.health.value().checks) : null,
  );
  protected readonly systemChecks = computed(() => this.split()?.other ?? []);
  protected readonly freshness = computed(() => {
    const rows = this.split()?.freshness ?? [];
    if (!rows.length) return null;
    const fresh = rows.filter((r) => r.level === 'good').length;
    return {
      ok: fresh === rows.length,
      detail: `${fresh} of ${rows.length} tickers have a recent price.`,
    };
  });

  protected refresh(): void {
    this.auto.refresh();
  }
}
