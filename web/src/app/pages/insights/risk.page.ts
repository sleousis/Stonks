import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  linkedSignal,
  resource,
} from '@angular/core';
import { RouterLink } from '@angular/router';

import { InsightsService } from '../../api/insights.service';
import type { RiskSnapshotView } from '../../api/models';
import { RiskService } from '../../api/risk.service';
import { SystemService } from '../../api/system.service';
import { formatMoney, formatNumber, formatPercent } from '../../core/format/format';
import { PortfolioContextService } from '../../core/portfolio/portfolio-context.service';
import type { ChartSeries } from '../../shared/chart/chart-engine';
import { TimeSeriesChart } from '../../shared/chart/time-series-chart';
import { UpdatedAgo, autoRefresh } from '../../shared/auto-refresh';
import { DataTable, TableCell, type TableColumn } from '../../shared/ui/data-table/data-table';
import { keepLatest } from '../../shared/ui/data-table/keep-latest';
import { strategyDisplayName } from '../../shared/strategy-names';
import { HelpTip } from '../../shared/ui/help-tip';
import { PageHeader } from '../../shared/ui/page-header';
import { NoBook, bookState } from '../../shared/ui/no-book';
import { EmptyState, ErrorState, LoadingState } from '../../shared/ui/states';
import { InsightsNav } from './insights-nav';
import { type LimitRow, STATUS_TEXT, limitRows } from './limit-rows';

const HISTORY_PAGE = 20;
/** Days drawn on the chart (the route's page limit). */
const CHART_DAYS = 200;

/** Below this Kupiec p-value the misses are more than bad luck. */
const KUPIEC_ALARM = 0.05;

/**
 * "3 misses, 1.2 times the expected rate", plus ", more often than chance
 * explains" when the Kupiec test says the model is off. No raw p-value.
 */
export function violationText(count: number, ratio: number | null, kupiec: number | null): string {
  const misses = `${count} ${count === 1 ? 'miss' : 'misses'}`;
  if (ratio == null) return misses;
  const alarm = kupiec != null && kupiec < KUPIEC_ALARM && ratio > 1;
  const tail = alarm ? ', more often than chance explains' : '';
  return `${misses}, ${formatNumber(ratio, { digits: 2 })} times the expected rate${tail}`;
}

/**
 * Risk of the picked portfolio: each measure against the limit it trades
 * under (your own limits where they are stricter), today's VaR and ES with
 * how often the model was wrong, each strategy's part with its alpha-decay
 * check, and the daily history.
 */
import { WhyNotPanel } from '../../shared/ui/why-not-panel';

@Component({
  selector: 'app-risk-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    RouterLink,
    PageHeader,
    InsightsNav,
    WhyNotPanel,
    DataTable,
    TableCell,
    HelpTip,
    TimeSeriesChart,
    UpdatedAgo,
    LoadingState,
    EmptyState,
    ErrorState,
    NoBook,
  ],
  templateUrl: './risk.page.html',
  styleUrl: './risk.page.scss',
})
export class RiskPage {
  private readonly riskApi = inject(RiskService);
  private readonly insightsApi = inject(InsightsService);
  private readonly systemApi = inject(SystemService);
  private readonly portfolioCtx = inject(PortfolioContextService);

  /** With no portfolio at all (UX-13) nothing asks for one. */
  protected readonly book = computed(() => bookState(this.portfolioCtx));
  private readonly bookParams = computed(() =>
    this.book() === 'ready' ? { portfolio: this.portfolioCtx.selectedId() } : undefined,
  );
  protected readonly live = resource({
    params: () => this.bookParams(),
    loader: () => this.riskApi.live(),
  });
  protected readonly insights = resource({
    params: () => this.bookParams(),
    loader: () => this.insightsApi.get(),
  });
  protected readonly policy = resource({ loader: () => this.systemApi.riskPolicy() });
  /** Your own limits on top of the system's; when it fails the system limits still show. */
  protected readonly mine = resource({ loader: () => this.riskApi.myLimits() });

  protected readonly chart = resource({
    params: () => this.bookParams(),
    loader: () => this.riskApi.snapshots({ limit: CHART_DAYS }),
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
    loader: ({ params }) => this.riskApi.snapshots({ limit: params.limit, offset: params.offset }),
  });
  protected readonly historyPage = keepLatest(this.history);
  protected readonly historyPageSize = HISTORY_PAGE;

  protected readonly auto = autoRefresh(() => [this.live, this.insights, this.chart, this.history]);

  /** Limits need the policy, plus insights and live risk for the readings. */
  protected readonly limitsLoading = computed(
    () =>
      (!this.policy.hasValue() && !this.policy.error()) ||
      (!this.mine.hasValue() && !this.mine.error()),
  );
  protected readonly rows = computed<LimitRow[]>(() => {
    const system = this.policy.hasValue() ? this.policy.value() : null;
    const effective = this.mine.hasValue() ? this.mine.value().effective : system;
    return limitRows(
      this.insights.hasValue() ? this.insights.value() : null,
      effective,
      this.live.hasValue() ? this.live.value() : null,
      system,
    );
  });
  /** At least one row follows your own, stricter limit. */
  protected readonly anyYours = computed(() => this.rows().some((r) => r.yours));
  /** Buys under your smallest order size are skipped: said once under the limits. */
  protected readonly smallestOrder = computed(() => {
    const v = this.mine.hasValue() ? this.mine.value().effective.min_order_notional : null;
    return v ? formatMoney(v) : null;
  });
  protected readonly statusText = STATUS_TEXT;
  protected readonly meterWidth = (r: LimitRow) =>
    `${Math.min(1, Math.max(0, r.usage ?? 0)) * 100}%`;

  protected readonly pct = (value: number | null | undefined) =>
    formatPercent(value, { digits: 2 });
  protected readonly money = (value: number | null | undefined) => formatMoney(value);
  protected readonly violationText = violationText;

  protected readonly chartSeries = computed<ChartSeries[]>(() => {
    const rows = this.chart.hasValue() ? [...this.chart.value().items].reverse() : [];
    return [
      {
        id: 'var95',
        label: 'VaR 95%',
        kind: 'line',
        color: 'primary',
        format: 'percent',
        points: rows
          .filter((r) => r.var_95 != null)
          .map((r) => ({ time: r.as_of, value: r.var_95! })),
      },
      {
        id: 'es95',
        label: 'ES 95%',
        kind: 'line',
        color: 'muted',
        format: 'percent',
        points: rows
          .filter((r) => r.es_95 != null)
          .map((r) => ({ time: r.as_of, value: r.es_95! })),
      },
      {
        id: 'loss',
        label: "Day's loss",
        kind: 'area',
        color: 'loss',
        pane: 1,
        format: 'percent',
        points: rows
          .filter((r) => r.realized_return != null)
          .map((r) => ({ time: r.as_of, value: -r.realized_return! })),
      },
    ];
  });
  protected readonly chartDays = computed(() =>
    this.chart.hasValue() ? this.chart.value().items.length : 0,
  );
  protected readonly chartSummary = computed(() => {
    const items = this.chart.hasValue() ? this.chart.value().items : [];
    const last = items[0];
    const first = items.at(-1);
    if (!last || !first) return null;
    const misses = items.filter((r) => r.violation_95).length;
    return (
      `One-day 95% VaR from ${first.as_of} to ${last.as_of}, latest ${this.pct(last.var_95)} ` +
      `of value. The day's loss beat VaR on ${misses} of ${items.length} days.`
    );
  });

  protected readonly sleeveColumns: TableColumn<RiskSnapshotView>[] = [
    {
      key: 'strategy_id',
      label: 'Strategy',
      mobile: 'title',
      value: (r) => (r.strategy_id ? strategyDisplayName(r.strategy_id) : 'Whole portfolio'),
    },
    { key: 'value', label: 'Value', format: 'money' },
    { key: 'var_95', label: 'VaR 95%', format: 'percent', help: 'var' },
    {
      key: 'es_95',
      label: 'ES 95%',
      format: 'percent',
      help: 'expected_shortfall',
      mobile: 'hide',
    },
    {
      key: 'violation_ratio_95',
      label: 'Violation ratio',
      format: 'number',
      help: 'violation_ratio',
    },
    { key: 'decayed', label: 'Edge', sortable: false, help: 'alpha_decay' },
  ];
  protected readonly sleeveKey = (r: RiskSnapshotView) => r.strategy_id ?? 'portfolio';

  protected readonly historyColumns: TableColumn<RiskSnapshotView>[] = [
    { key: 'as_of', label: 'Day', format: 'date', mobile: 'title' },
    { key: 'value', label: 'Value', format: 'money' },
    { key: 'realized_return', label: 'Return', format: 'signedPercent', tone: true },
    { key: 'var_95', label: 'VaR 95%', format: 'percent', help: 'var' },
    {
      key: 'es_95',
      label: 'ES 95%',
      format: 'percent',
      help: 'expected_shortfall',
      mobile: 'hide',
    },
    { key: 'violation_95', label: 'Beat VaR', sortable: false },
  ];
  protected readonly historyKey = (r: RiskSnapshotView) => `${r.as_of}:${r.strategy_id ?? ''}`;
}
