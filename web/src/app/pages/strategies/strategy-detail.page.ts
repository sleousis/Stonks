import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  input,
  linkedSignal,
  resource,
  signal,
  viewChild,
} from '@angular/core';
import { Router, RouterLink } from '@angular/router';

import type {
  OrderView,
  PnlRowView,
  ShadowDecisionView,
  StatusChangeView,
  StrategyDetail,
  StrategyMetadataView,
  SurvivalReportView,
} from '../../api/models';
import { OrdersService } from '../../api/orders.service';
import { StrategiesService } from '../../api/strategies.service';
import { SubscriptionsService } from '../../api/subscriptions.service';
import { SystemService } from '../../api/system.service';
import { SessionService } from '../../core/auth/session.service';
import {
  formatDate,
  formatDateTime,
  formatMoney,
  formatNumber,
  formatPercent,
} from '../../core/format/format';
import { ToastService } from '../../core/notify/toast.service';
import { PortfolioContextService } from '../../core/portfolio/portfolio-context.service';
import { TimeSeriesChart } from '../../shared/chart/time-series-chart';
import { checkRow } from '../../shared/golive-checks';
import { demoteOptions, isRealMoneyBroker, promoteThroughGate } from '../../shared/governance';
import {
  LIFECYCLE,
  type LifecycleAction,
  STATUS_MEANING,
  STATUS_WORDS,
} from '../../shared/governance-labels';
import { testLabel } from '../../shared/lab-results/survival-tests';
import { formatMetric, metricLabel } from '../../shared/metrics';
import { type Verdict, strategyVerdict } from '../../shared/strategy-verdict';
import { DataTable, TableCell, type TableColumn } from '../../shared/ui/data-table/data-table';
import { HelpTip } from '../../shared/ui/help-tip';
import { ModeStamp } from '../../shared/ui/mode-stamp';
import { MonthlyReturns } from '../../shared/ui/monthly-returns';
import { humanize } from '../../shared/ui/param-form/param-spec';
import { PageHeader } from '../../shared/ui/page-header';
import { PermissionNote } from '../../shared/ui/permission-note';
import { Segmented } from '../../shared/ui/segmented';
import { SideTag } from '../../shared/ui/side-tag';
import { StatTile } from '../../shared/ui/stat-tile';
import { ErrorState, EmptyState, LoadingState } from '../../shared/ui/states';
import { StatusChangeDialog } from '../../shared/ui/status-change-dialog';
import { StatusPill } from '../../shared/ui/status-pill';
import { StrategyVerdict } from '../../shared/ui/strategy-verdict';
import { OrderStatus } from '../orders/order-status';
import { FollowPanel } from './follow-panel';
import { GoliveCheckList } from './golive-check-list';
import { ModelVersionsPanel } from './model-versions-panel';
import { StageBar } from './stage-bar';
import { formatParam, strategyDisplayName, strategyKindName } from './strategy-format';
import { curveSeries, curveSummary } from './tearsheet-data';

/**
 * The strategy page's tabs (F27): one place to judge a strategy.
 * Overview (verdict, trial result, follow), Results (the tear sheet's
 * figures), Review (the go-live check and status changes), Details (why it
 * should work, parameters, robustness tests, history) and Model versions.
 */
export type DetailTab = 'overview' | 'results' | 'review' | 'details' | 'versions';

const TABS: readonly DetailTab[] = ['overview', 'results', 'review', 'details', 'versions'];

export function asTab(value: string | null | undefined): DetailTab {
  return TABS.includes(value as DetailTab) ? (value as DetailTab) : 'overview';
}

/** A status action: the lifecycle steps, plus the admin's override (UX-23). */
export type DetailAction = LifecycleAction | 'override';

export interface ActionButton {
  key: DetailAction;
  label: string;
  primary: boolean;
}

function button(key: DetailAction, label: string, primary = false): ActionButton {
  return { key, label, primary };
}

/**
 * The header's one forward step (UX-23, M9):
 * - retired: Put on trial;
 * - approved: nothing (stepping back lives on the Review tab);
 * - on trial: Approve (primary) once the go-live check passed, otherwise an
 *   "Override..." for those who may approve.
 * Retire is never here: it is a quiet button on the Review tab, far from the
 * kill switch at the top of every page.
 */
export function detailActions(
  status: StrategyDetail['status'],
  golivePassed: boolean | null,
  canPromote: boolean,
): ActionButton[] {
  if (status === 'retired') return [button('paper', LIFECYCLE.paper.label, true)];
  if (status === 'active') return [];
  if (golivePassed === true) return [button('live', LIFECYCLE.live.label, true)];
  return canPromote ? [button('override', 'Override…')] : [];
}

/** The quiet steps back, on the Review tab: Back on trial and Retire. Never red. */
export function statusActions(status: StrategyDetail['status']): ActionButton[] {
  if (status === 'active') {
    return [button('pause', LIFECYCLE.pause.label), button('stop', LIFECYCLE.stop.label)];
  }
  if (status === 'shadow') return [button('stop', LIFECYCLE.stop.label)];
  return [];
}

/** Recent orders shown on the page; the Orders page has the rest. */
export const RECENT_ORDERS = 10;

/** A trial (test book) result from its daily value series. */
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

/** Who changed a status, as people read it: the system is "Stonks" (F31). */
export function actorName(actor: string): string {
  if (!actor || /^(service|system)(:|$)/i.test(actor)) return 'Stonks';
  return actor.replace(/^user:/i, '');
}

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
      actor: actorName(h.actor),
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

const NA = 'n/a';

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
    StrategyVerdict,
    FollowPanel,
    GoliveCheckList,
    ModelVersionsPanel,
    MonthlyReturns,
    PermissionNote,
    Segmented,
    StatTile,
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
  private readonly ordersApi = inject(OrdersService);
  private readonly systemApi = inject(SystemService);
  private readonly subscriptionsApi = inject(SubscriptionsService);
  private readonly portfolioCtx = inject(PortfolioContextService);
  private readonly toasts = inject(ToastService);
  protected readonly session = inject(SessionService);
  private readonly dialog = viewChild.required(StatusChangeDialog);

  private readonly router = inject(Router);

  /** Route param `:id`. */
  readonly id = input.required<string>();
  /** Query param `?tab=results|review|details|versions` opens that tab. */
  readonly tab = input<string | undefined>(undefined);

  /** Model versions are a builder's tool: shown to those who may run the Lab (F29). */
  protected readonly canSeeVersions = computed(
    () => this.session.can('lab.run') || this.session.can('strategy.promote'),
  );
  protected readonly tabs = computed(() => [
    { value: 'overview' as const, label: 'Overview' },
    { value: 'results' as const, label: 'Results' },
    { value: 'review' as const, label: 'Review' },
    { value: 'details' as const, label: 'Details' },
    ...(this.canSeeVersions() ? [{ value: 'versions' as const, label: 'Model versions' }] : []),
  ]);
  protected readonly shownTab = linkedSignal<DetailTab>(() => asTab(this.tab()));

  /** Switch tabs and keep the choice in the address, so a link opens it. */
  protected setTab(value: string): void {
    const tab = asTab(value);
    this.shownTab.set(tab);
    void this.router.navigate([], {
      queryParams: { tab: tab === 'overview' ? null : tab },
      queryParamsHandling: 'merge',
      replaceUrl: true,
    });
  }

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

  /**
   * Its trial record, whatever its status (F28): the test book's figures,
   * value curve, monthly returns, recent trades and the go-live check. An
   * approved strategy keeps showing the record it earned on trial.
   */
  protected readonly sheet = resource({
    params: () => ({ id: this.id() }),
    loader: ({ params }) => this.strategiesApi.tearSheet(params.id),
  });
  private readonly sheetValue = computed(() => (this.sheet.hasValue() ? this.sheet.value() : null));

  protected readonly busy = signal<DetailAction | null>(null);

  protected readonly detail = computed(() =>
    this.strategy.hasValue() ? this.strategy.value() : null,
  );
  private readonly status = computed(() => this.detail()?.status ?? null);
  /** A name to read, not the registry id (UX-27). The id sits under Technical details. */
  protected readonly displayName = computed(() =>
    strategyDisplayName(this.detail()?.id ?? this.id(), { starter: this.detail()?.starter }),
  );

  /**
   * The broker, read for an approved strategy: its title carries the LIVE
   * stamp only when followers' orders reach a real-money account.
   */
  protected readonly broker = resource({
    params: () => (this.status() === 'active' ? { live: true } : undefined),
    loader: () => this.systemApi.broker(),
  });
  protected readonly realMoney = computed(
    () => this.broker.hasValue() && isRealMoneyBroker(this.broker.value()),
  );

  /** The go-live check, from the same read as the trial record. */
  protected readonly goliveReport = computed(() => this.sheetValue()?.golive ?? null);
  protected readonly golivePassed = computed(() => this.goliveReport()?.passed ?? null);

  /** One plain verdict (F33): worth following, promising, or not good enough yet. */
  protected readonly verdict = computed<Verdict | null>(() => {
    const s = this.detail();
    const sheet = this.sheetValue();
    if (!s || !sheet) return null;
    const reports = s.survival_reports;
    return strategyVerdict({
      status: s.status,
      golive: sheet.golive,
      trial: {
        total_return: sheet.paper.total_return ?? null,
        max_drawdown: sheet.paper.max_drawdown ?? null,
        days: sheet.paper.days,
      },
      tests: reports.length
        ? { passed: reports.filter((r) => r.passed).length, total: reports.length }
        : null,
    });
  });
  /** The verdict's Details fold: each check's value against its limit. */
  protected readonly verdictChecks = computed(() => {
    const g = this.goliveReport();
    return g ? g.checks.map((c) => checkRow(c, g.strategy_id)) : [];
  });

  protected readonly curve = computed(() => this.sheetValue()?.curve ?? []);
  protected readonly performance = computed(() => paperPerformance(this.curve()));
  protected readonly chartSeries = computed(() => curveSeries(this.curve()));
  protected readonly chartSummary = computed(() => {
    const p = this.performance();
    if (!p) return null;
    return (
      `Its test book went from ${formatMoney(p.firstValue)} on ${formatDate(p.firstDay)} to ` +
      `${formatMoney(p.lastValue)} on ${formatDate(p.lastDay)}, a return of ` +
      `${formatPercent(p.totalReturn, { signed: true })}. ` +
      `The worst drop was ${formatPercent(p.maxDrawdown)}.`
    );
  });
  protected readonly resultsSummary = computed(() => curveSummary(this.curve()));

  /** The Results tab's figures (the tear sheet's tiles). */
  protected readonly tiles = computed(() => {
    const sheet = this.sheetValue();
    if (!sheet) return [];
    const p = sheet.paper;
    const pct = (v: number | null | undefined, signed = false) =>
      v === null || v === undefined ? NA : formatPercent(v, { signed });
    const num = (v: number | null | undefined) =>
      v === null || v === undefined ? NA : formatNumber(v, { digits: 2 });
    return [
      { label: 'Total return', value: pct(p.total_return, true), help: 'total_return' },
      { label: 'CAGR', value: pct(p.cagr, true), help: 'cagr' },
      { label: 'Sharpe', value: num(p.sharpe), help: 'sharpe' },
      { label: 'Max drawdown', value: pct(p.max_drawdown), help: 'max_drawdown' },
      { label: 'Volatility', value: pct(p.volatility), help: 'volatility' },
      { label: 'Days on trial', value: formatNumber(p.days), help: false as const },
      {
        label: 'Trades',
        value: formatNumber(p.trades + sheet.book_trades),
        help: false as const,
      },
    ];
  });
  protected readonly tradeColumns: TableColumn<ShadowDecisionView>[] = [
    { key: 'as_of', label: 'Date', format: 'date', mobile: 'title' },
    { key: 'side', label: 'Side', sortable: false },
    { key: 'ticker', label: 'Ticker' },
    { key: 'quantity', label: 'Qty', format: 'number' },
    { key: 'price', label: 'Price', format: 'money' },
    { key: 'status', label: 'Status', mobile: 'hide' },
  ];
  protected readonly tradeKey = (d: ShadowDecisionView) => String(d.id);

  /** Orders it placed in the picked portfolio, newest first. */
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
      : 'Verdict, trial results and how to follow it.';
  });
  protected readonly meaning = computed(() => {
    const s = this.detail();
    return s ? STATUS_MEANING[s.status] : '';
  });

  protected readonly canPromote = computed(() => this.session.can('strategy.promote'));

  /** The header's forward step, in the order offered. */
  protected readonly actions = computed<ActionButton[]>(() => {
    const s = this.detail();
    return s ? detailActions(s.status, this.golivePassed(), this.canPromote()) : [];
  });
  /** The quiet steps back on the Review tab. */
  protected readonly backActions = computed<ActionButton[]>(() => {
    const s = this.detail();
    return s ? statusActions(s.status) : [];
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
    this.sheet.reload();
  }

  /** Names of your portfolios that trade it (not alerts only), for the ticket. */
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
      title: `Approve ${name}?`,
      message: 'People can follow it from the next trading run.',
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
            title: `Put ${name} on trial?`,
            message:
              'The system paper-tests it on its own test book from the next trading run. ' +
              'No portfolio trades it until someone approves it.',
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
