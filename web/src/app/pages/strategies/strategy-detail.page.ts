import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  input,
  resource,
  signal,
  viewChild,
} from '@angular/core';
import { RouterLink } from '@angular/router';

import type {
  OrderView,
  PnlRowView,
  StatusChangeView,
  StrategyMetadataView,
  SurvivalReportView,
} from '../../api/models';
import { OrdersService } from '../../api/orders.service';
import { ShadowService } from '../../api/shadow.service';
import { StrategiesService } from '../../api/strategies.service';
import { SessionService } from '../../core/auth/session.service';
import { formatDate, formatDateTime, formatMoney, formatPercent } from '../../core/format/format';
import { ToastService } from '../../core/notify/toast.service';
import { PortfolioContextService } from '../../core/portfolio/portfolio-context.service';
import type { ChartSeries } from '../../shared/chart/chart-engine';
import { TimeSeriesChart } from '../../shared/chart/time-series-chart';
import { promoteThroughGate } from '../../shared/governance';
import { LIFECYCLE, type LifecycleAction, STATUS_WORDS } from '../../shared/governance-labels';
import { testLabel } from '../../shared/lab-results/survival-tests';
import { formatMetric, metricLabel } from '../../shared/metrics';
import { DataTable, TableCell, type TableColumn } from '../../shared/ui/data-table/data-table';
import { HelpTip } from '../../shared/ui/help-tip';
import { PageHeader } from '../../shared/ui/page-header';
import { PermissionNote } from '../../shared/ui/permission-note';
import { SideTag } from '../../shared/ui/side-tag';
import { ErrorState, EmptyState, LoadingState } from '../../shared/ui/states';
import { StatusChangeDialog } from '../../shared/ui/status-change-dialog';
import { StatusPill } from '../../shared/ui/status-pill';
import { StageBar } from './stage-bar';
import { formatParam, strategyKindName } from './strategy-format';

/** Recent orders shown on the page; the Orders page has the rest. */
export const RECENT_ORDERS = 10;

/** Paper performance from a shadow P&L series. */
export interface PaperPerformance {
  totalReturn: number | null;
  maxDrawdown: number | null;
  days: number;
  firstValue: number;
  lastValue: number;
  firstDay: string;
  lastDay: string;
}

export function paperPerformance(rows: readonly PnlRowView[]): PaperPerformance | null {
  if (!rows.length) return null;
  const first = rows[0];
  const last = rows[rows.length - 1];
  return {
    totalReturn: last.cumulative_return ?? null,
    maxDrawdown: rows.reduce((worst, r) => Math.min(worst, r.drawdown), 0),
    days: last.days_elapsed ?? rows.length,
    firstValue: first.total_value,
    lastValue: last.total_value,
    firstDay: first.day,
    lastDay: last.day,
  };
}

/** Research-card labels for the metadata enums. */
export const ALPHA_FAMILY_LABELS: Record<StrategyMetadataView['alpha_family'], string> = {
  trend: 'Trend',
  reversion: 'Mean reversion',
  carry: 'Carry',
  value: 'Value',
  quality: 'Quality',
  growth: 'Growth',
  sentiment: 'Sentiment',
  data_driven: 'Data-driven',
  benchmark: 'Benchmark',
  other: 'Other',
};

const PREMISE_LABELS: Record<StrategyMetadataView['premise'], string> = {
  trend: 'Trends persist',
  mean_reversion: 'Prices return to a mean',
  none: 'None stated',
};

export interface HistoryEntry {
  id: number;
  when: string;
  from: string | null;
  to: string | null;
  kind: string;
  actor: string;
  reason: string;
  override: boolean;
  golive: boolean | null;
}

/** Newest first, for the timeline. */
export function historyEntries(items: readonly StatusChangeView[]): HistoryEntry[] {
  return [...items]
    .sort((a, b) => b.created_at.localeCompare(a.created_at) || b.id - a.id)
    .map((h) => ({
      id: h.id,
      when: h.created_at,
      from: h.from_status,
      to: h.to_status,
      kind: h.kind,
      actor: h.actor,
      reason: h.reason,
      override: h.override,
      golive: h.golive_passed,
    }));
}

function metricList(report: SurvivalReportView): { key: string; label: string; value: string }[] {
  return Object.entries(report.metrics).map(([key, value]) => ({
    key,
    label: metricLabel(key),
    value: formatMetric(key, value),
  }));
}

@Component({
  selector: 'app-strategy-detail-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    HelpTip,
    RouterLink,
    PageHeader,
    StatusPill,
    StatusChangeDialog,
    StageBar,
    PermissionNote,
    TimeSeriesChart,
    DataTable,
    TableCell,
    SideTag,
    LoadingState,
    EmptyState,
    ErrorState,
  ],
  templateUrl: './strategy-detail.page.html',
  styleUrl: './strategy-detail.page.scss',
})
export class StrategyDetailPage {
  private readonly strategiesApi = inject(StrategiesService);
  private readonly shadowApi = inject(ShadowService);
  private readonly ordersApi = inject(OrdersService);
  private readonly portfolioCtx = inject(PortfolioContextService);
  private readonly toasts = inject(ToastService);
  protected readonly session = inject(SessionService);
  private readonly dialog = viewChild.required(StatusChangeDialog);

  /** Route param `:id`. */
  readonly id = input.required<string>();

  protected readonly strategy = resource({
    params: () => ({ id: this.id() }),
    loader: ({ params }) => this.strategiesApi.get(params.id),
  });

  /** Audited status changes: its own panel, so a failure never blanks the page. */
  protected readonly history = resource({
    params: () => ({ id: this.id() }),
    loader: ({ params }) => this.strategiesApi.history(params.id),
  });
  protected readonly timeline = computed(() =>
    this.history.hasValue() ? historyEntries(this.history.value()) : [],
  );

  protected readonly busy = signal<LifecycleAction | null>(null);

  protected readonly detail = computed(() =>
    this.strategy.hasValue() ? this.strategy.value() : null,
  );
  private readonly status = computed(() => this.detail()?.status ?? null);

  /** The go-live verdict of a paper strategy, for the stage bar. */
  protected readonly golive = resource({
    params: () => (this.status() === 'shadow' ? { id: this.id() } : undefined),
    loader: ({ params }) => this.strategiesApi.golive(params.id),
  });
  protected readonly golivePassed = computed(() =>
    this.golive.hasValue() ? this.golive.value().passed : null,
  );

  /** Its paper book's daily value (shadow strategies only). */
  protected readonly pnl = resource({
    params: () => (this.status() === 'shadow' ? { id: this.id() } : undefined),
    loader: ({ params }) => this.shadowApi.pnl(params.id),
  });
  protected readonly performance = computed(() =>
    this.pnl.hasValue() ? paperPerformance(this.pnl.value().rows) : null,
  );
  protected readonly chartSeries = computed<ChartSeries[]>(() => {
    const rows = this.pnl.hasValue() ? this.pnl.value().rows : [];
    return [
      {
        id: 'value',
        label: 'Paper value',
        kind: 'line',
        color: 'primary',
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
    const p = this.performance();
    if (!p) return null;
    return (
      `Paper value went from ${formatMoney(p.firstValue)} on ${formatDate(p.firstDay)} to ` +
      `${formatMoney(p.lastValue)} on ${formatDate(p.lastDay)}, a return of ` +
      `${formatPercent(p.totalReturn, { signed: true })}. ` +
      `The worst drawdown was ${formatPercent(p.maxDrawdown)}.`
    );
  });

  /** Orders it placed, newest first, for the picked portfolio. */
  protected readonly orders = resource({
    params: () => ({ id: this.id(), portfolio: this.portfolioCtx.selectedId() }),
    loader: ({ params }) => this.ordersApi.list({ strategy_id: params.id, limit: RECENT_ORDERS }),
  });
  protected readonly orderColumns: TableColumn<OrderView>[] = [
    { key: 'ticker', label: 'Ticker', mobile: 'title' },
    { key: 'side', label: 'Side' },
    { key: 'quantity', label: 'Qty', format: 'number' },
    { key: 'status', label: 'Status' },
    { key: 'created_at', label: 'Placed', format: 'datetime' },
  ];
  protected readonly orderKey = (o: OrderView) => o.client_id;

  protected readonly kindName = computed(() => {
    const s = this.detail();
    return s ? strategyKindName(s.class_path) : null;
  });

  protected readonly description = computed(() => {
    const s = this.detail();
    return s
      ? `${strategyKindName(s.class_path)}. ${STATUS_WORDS[s.status]}.`
      : 'Status, parameters and survival evidence.';
  });

  protected readonly canPromote = computed(() => this.session.can('strategy.promote'));

  /** Actions that change the current status, in the order they are offered. */
  protected readonly actions = computed<{ key: LifecycleAction; label: string }[]>(() => {
    const s = this.detail();
    if (!s) return [];
    const toPaper: LifecycleAction = s.status === 'active' ? 'pause' : 'paper';
    return (['live', toPaper, 'stop'] as const)
      .filter((key) => LIFECYCLE[key].target !== s.status)
      .map((key) => ({ key, label: LIFECYCLE[key].label }));
  });

  protected readonly research = computed(() => {
    const m = this.detail()?.metadata;
    if (!m) return null;
    return {
      hypothesis: m.hypothesis.trim(),
      family: ALPHA_FAMILY_LABELS[m.alpha_family] ?? m.alpha_family,
      premise: PREMISE_LABELS[m.premise] ?? m.premise,
      horizon: m.label_horizon_bars,
      history: m.required_history_bars,
    };
  });

  protected readonly params = computed(() => {
    const s = this.detail();
    if (!s) return [];
    return Object.entries(s.params).map(([key, value]) => ({ key, value: formatParam(value) }));
  });

  protected readonly reports = computed(() =>
    (this.detail()?.survival_reports ?? []).map((r) => ({
      ...r,
      label: testLabel(r.test_id),
      metricList: metricList(r),
    })),
  );

  protected readonly reportSummary = computed(() => {
    const reports = this.detail()?.survival_reports ?? [];
    if (!reports.length) return null;
    const passed = reports.filter((r) => r.passed).length;
    return {
      passed,
      total: reports.length,
      allPassed: passed === reports.length,
      label: `${passed} of ${reports.length} passed`,
    };
  });

  protected readonly date = formatDate;
  protected readonly dateTime = formatDateTime;
  protected pct(value: number | null, signed = false): string {
    return formatPercent(value, { signed });
  }

  protected async run(action: LifecycleAction): Promise<void> {
    const s = this.detail();
    if (!s || this.busy() || !this.canPromote()) return;
    const done = action === 'live' ? await this.promote(s.id) : await this.demote(action, s);
    if (!done) return;
    this.strategy.reload();
    this.history.reload();
  }

  private async promote(id: string): Promise<boolean> {
    const result = await promoteThroughGate({
      id,
      dialog: this.dialog(),
      toasts: this.toasts,
      golive: () => this.strategiesApi.golive(id),
      promote: (body) => this.strategiesApi.promote(id, body, true),
      title: `Go live with ${id}?`,
      message: 'It places orders through the broker from the next trading run.',
      confirmLabel: LIFECYCLE.live.label,
      busy: (on) => this.busy.set(on ? 'live' : null),
    });
    if (!result) return false;
    this.toasts.success(LIFECYCLE.live.done(id));
    return true;
  }

  private async demote(action: LifecycleAction, s: { id: string; status: string }) {
    const stop = action === 'stop';
    const words = LIFECYCLE[action];
    const body = await this.dialog().open(
      stop
        ? {
            title: `Stop ${s.id}?`,
            message:
              'It stops trading and stops paper decisions from the next run. Its reports and history stay.',
            confirmLabel: words.label,
            tone: 'danger',
            minReason: 1,
          }
        : {
            title:
              s.status === 'active'
                ? `Move ${s.id} back to paper trading?`
                : `Start paper trading ${s.id}?`,
            message:
              s.status === 'active'
                ? 'It stops placing orders from the next run and keeps making paper decisions you can compare in Shadow.'
                : 'It makes paper decisions from the next run without placing orders.',
            confirmLabel: words.label,
            minReason: 1,
          },
    );
    if (!body) return false;
    this.busy.set(action);
    try {
      await (stop ? this.strategiesApi.retire(s.id, body) : this.strategiesApi.shadow(s.id, body));
      this.toasts.success(words.done(s.id));
      return true;
    } catch {
      return false; // The error interceptor already showed the API's message.
    } finally {
      this.busy.set(null);
    }
  }
}
