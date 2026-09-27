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
  StrategyDetail,
  StrategyMetadataView,
  SurvivalReportView,
} from '../../api/models';
import { OrdersService } from '../../api/orders.service';
import { ShadowService } from '../../api/shadow.service';
import { StrategiesService } from '../../api/strategies.service';
import { SubscriptionsService } from '../../api/subscriptions.service';
import { SystemService } from '../../api/system.service';
import { SessionService } from '../../core/auth/session.service';
import { formatDate, formatDateTime, formatMoney, formatPercent } from '../../core/format/format';
import { ToastService } from '../../core/notify/toast.service';
import { PortfolioContextService } from '../../core/portfolio/portfolio-context.service';
import type { ChartSeries } from '../../shared/chart/chart-engine';
import { TimeSeriesChart } from '../../shared/chart/time-series-chart';
import { demoteOptions, isRealMoneyBroker, promoteThroughGate } from '../../shared/governance';
import { LIFECYCLE, type LifecycleAction, STATUS_WORDS } from '../../shared/governance-labels';
import { testLabel } from '../../shared/lab-results/survival-tests';
import { formatMetric, metricLabel } from '../../shared/metrics';
import { DataTable, TableCell, type TableColumn } from '../../shared/ui/data-table/data-table';
import { HelpTip } from '../../shared/ui/help-tip';
import { ModeStamp } from '../../shared/ui/mode-stamp';
import { humanize } from '../../shared/ui/param-form/param-spec';
import { PageHeader } from '../../shared/ui/page-header';
import { PermissionNote } from '../../shared/ui/permission-note';
import { SideTag } from '../../shared/ui/side-tag';
import { ErrorState, EmptyState, LoadingState } from '../../shared/ui/states';
import { StatusChangeDialog } from '../../shared/ui/status-change-dialog';
import { StatusPill } from '../../shared/ui/status-pill';
import { OrderStatus } from '../orders/order-status';
import { FollowPanel } from './follow-panel';
import { StageBar } from './stage-bar';
import { formatParam, strategyDisplayName, strategyKindName } from './strategy-format';

/** A header action: the lifecycle steps, plus the admin's override (UX-23). */
export type DetailAction = LifecycleAction | 'override';

export interface ActionButton {
  key: DetailAction;
  label: string;
  primary: boolean;
  danger: boolean;
}

/**
 * The header actions for a strategy (UX-23):
 * - stopped: only Start paper trading;
 * - live: Back to paper trading and Stop;
 * - paper trading: Go live (primary) only once the go-live check passed,
 *   otherwise an "Override..." for those who may go live, and Stop.
 */
export function detailActions(
  status: StrategyDetail['status'],
  golivePassed: boolean | null,
  canPromote: boolean,
): ActionButton[] {
  const button = (key: DetailAction, label: string, primary = false): ActionButton => ({
    key,
    label,
    primary,
    danger: key === 'stop',
  });
  if (status === 'retired') return [button('paper', LIFECYCLE.paper.label, true)];
  if (status === 'active') {
    return [button('pause', LIFECYCLE.pause.label), button('stop', LIFECYCLE.stop.label)];
  }
  const actions: ActionButton[] = [];
  if (golivePassed === true) actions.push(button('live', LIFECYCLE.live.label, true));
  else if (canPromote) actions.push(button('override', 'Override…'));
  actions.push(button('stop', LIFECYCLE.stop.label));
  return actions;
}

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
    ModeStamp,
    RouterLink,
    PageHeader,
    StatusPill,
    StatusChangeDialog,
    StageBar,
    FollowPanel,
    PermissionNote,
    TimeSeriesChart,
    DataTable,
    TableCell,
    SideTag,
    OrderStatus,
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
  private readonly systemApi = inject(SystemService);
  private readonly subscriptionsApi = inject(SubscriptionsService);
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

  protected readonly busy = signal<DetailAction | null>(null);

  protected readonly detail = computed(() =>
    this.strategy.hasValue() ? this.strategy.value() : null,
  );
  private readonly status = computed(() => this.detail()?.status ?? null);
  /** A name to read, not the registry id (UX-27). The id sits under Technical details. */
  protected readonly displayName = computed(() =>
    strategyDisplayName(this.detail()?.id ?? this.id()),
  );

  /**
   * The broker, read for a live strategy: its title carries the LIVE stamp
   * when orders reach a real-money account (docs/ui.md: a stamp, not a pill).
   */
  protected readonly broker = resource({
    params: () => (this.status() === 'active' ? { live: true } : undefined),
    loader: () => this.systemApi.broker(),
  });
  protected readonly realMoney = computed(
    () => this.broker.hasValue() && isRealMoneyBroker(this.broker.value()),
  );

  /** The go-live verdict of a paper strategy, for the stage bar. */
  protected readonly golive = resource({
    params: () => (this.status() === 'shadow' ? { id: this.id() } : undefined),
    loader: ({ params }) => this.strategiesApi.golive(params.id),
  });
  protected readonly golivePassed = computed(() =>
    this.golive.hasValue() ? this.golive.value().passed : null,
  );
  protected readonly goliveReport = computed(() =>
    this.golive.hasValue() ? this.golive.value() : null,
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
      : 'Status, parameters and robustness tests.';
  });

  protected readonly canPromote = computed(() => this.session.can('strategy.promote'));

  /** Actions that change the current status, in the order they are offered. */
  protected readonly actions = computed<ActionButton[]>(() => {
    const s = this.detail();
    return s ? detailActions(s.status, this.golivePassed(), this.canPromote()) : [];
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
    return Object.entries(s.params).map(([key, value]) => ({
      key,
      label: humanize(key),
      value: formatParam(value),
    }));
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

  protected async run(action: DetailAction): Promise<void> {
    const s = this.detail();
    if (!s || this.busy() || !this.canPromote()) return;
    const done =
      action === 'live' || action === 'override'
        ? await this.promote(s.id, action === 'override')
        : await this.demote(action, s);
    if (!done) return;
    this.strategy.reload();
    this.history.reload();
  }

  /** Names of your portfolios that paper trade or auto trade it, for the ticket. */
  private async followers(id: string): Promise<string[]> {
    const subs = await this.subscriptionsApi.list();
    const names = this.portfolioCtx.options();
    return subs
      .filter((s) => s.strategy_id === id && s.portfolio_id && s.mode !== 'notify')
      .map((s) => names.find((p) => p.id === s.portfolio_id)?.name ?? 'One of your portfolios');
  }

  private async promote(id: string, overrideFirst: boolean): Promise<boolean> {
    const name = this.displayName();
    const result = await promoteThroughGate({
      id,
      name,
      dialog: this.dialog(),
      toasts: this.toasts,
      golive: () => this.strategiesApi.golive(id),
      promote: (body) => this.strategiesApi.promote(id, body, true),
      broker: () => this.systemApi.broker(),
      followers: () => this.followers(id),
      overrideFirst,
      title: `Go live with ${name}?`,
      message: 'It places orders through the broker from the next trading run.',
      confirmLabel: LIFECYCLE.live.label,
      busy: (on) => this.busy.set(on ? (overrideFirst ? 'override' : 'live') : null),
    });
    if (!result) return false;
    this.toasts.success(LIFECYCLE.live.done(name));
    return true;
  }

  private async demote(action: LifecycleAction, s: { id: string }) {
    const name = this.displayName();
    const words = LIFECYCLE[action];
    const body = await this.dialog().open(
      action === 'pause' || action === 'stop'
        ? demoteOptions(action, name)
        : {
            title: `Start paper trading ${name}?`,
            message: 'It makes paper decisions from the next run without placing orders.',
            confirmLabel: words.label,
            minReason: 1,
          },
    );
    if (!body) return false;
    this.busy.set(action);
    try {
      await (action === 'stop'
        ? this.strategiesApi.retire(s.id, body)
        : this.strategiesApi.shadow(s.id, body));
      this.toasts.success(words.done(name));
      return true;
    } catch {
      return false; // The error interceptor already showed the API's message.
    } finally {
      this.busy.set(null);
    }
  }
}
