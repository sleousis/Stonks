import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  resource,
  signal,
  viewChild,
} from '@angular/core';

import { RouterLink } from '@angular/router';

import { HealthService } from '../../api/health.service';
import { IngestService } from '../../api/ingest.service';
import type { IngestRunView, TickRun } from '../../api/models';
import { TicksService } from '../../api/ticks.service';
import { SessionService } from '../../core/auth/session.service';
import { ConfirmService } from '../../core/confirm/confirm.service';
import { formatAgo, formatDateTime } from '../../core/format/format';
import { HaltStateService } from '../../core/halts/halt-state.service';
import { ToastService } from '../../core/notify/toast.service';
import { UpdatedAgo, autoRefresh } from '../../shared/auto-refresh';
import { DataTable, TableCell, type TableColumn } from '../../shared/ui/data-table/data-table';
import { AlertsPanel } from '../../shared/ui/alerts-panel';
import { PageHeader } from '../../shared/ui/page-header';
import { PermissionNote } from '../../shared/ui/permission-note';
import { StatTile, type StatTone } from '../../shared/ui/stat-tile';
import { EmptyState, ErrorState, LoadingState } from '../../shared/ui/states';
import { StatusPill } from '../../shared/ui/status-pill';
import { runWord } from '../../core/schedule/run-status';
import { kindLabel, sourceLabel } from '../data/data-labels';
import { parseTickers } from '../data/ingest-request';
import { GatewayPanel } from './gateway-panel';
import {
  CHECK_ACTIONS,
  type FreshnessRow,
  type HealthLevel,
  LEVEL_LABEL,
  LEVEL_TONE,
  LEVEL_URGENCY,
  checkLevel,
  checkMeaning,
  checkPill,
  checkThreshold,
  checkTitle,
  plainDetail,
  splitChecks,
  worstLevel,
} from './health-state';
import { ReconcilePanel } from './reconcile-panel';

const RECENT_FAILURES = 10;

/**
 * Is the system well: data freshness, stuck runs, recent data update and
 * trading run failures, and the system alerts. Reloads every minute; failed
 * trading runs link to their page.
 */
@Component({
  selector: 'app-health-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    RouterLink,
    PageHeader,
    StatTile,
    StatusPill,
    DataTable,
    TableCell,
    LoadingState,
    EmptyState,
    ErrorState,
    AlertsPanel,
    GatewayPanel,
    ReconcilePanel,
    PermissionNote,
    UpdatedAgo,
  ],
  templateUrl: './health.page.html',
  styleUrl: './health.page.scss',
})
export class HealthPage {
  private readonly healthApi = inject(HealthService);
  private readonly ingestApi = inject(IngestService);
  private readonly ticksApi = inject(TicksService);
  private readonly confirm = inject(ConfirmService);
  private readonly toasts = inject(ToastService);
  private readonly halts = inject(HaltStateService);
  protected readonly session = inject(SessionService);

  /** Running the checks can open or clear the operational halt: admins only. */
  protected readonly canRunChecks = computed(() => this.session.can('operations.run'));
  protected readonly runningChecks = signal(false);

  /** Tickers for the freshness checks; empty means the production universe. */
  protected readonly tickers = signal<string[]>([]);

  protected readonly report = resource({
    params: () => ({ tickers: this.tickers() }),
    loader: ({ params }) =>
      this.healthApi.report(params.tickers.length ? { tickers: params.tickers } : undefined),
  });
  protected readonly version = resource({ loader: () => this.healthApi.ping() });
  protected readonly failedIngest = resource({
    loader: () => this.ingestApi.runs({ status: 'error', limit: RECENT_FAILURES }),
  });
  protected readonly failedTicks = resource({
    loader: () => this.ticksApi.list({ status: 'error', limit: RECENT_FAILURES }),
  });

  private readonly alertsPanel = viewChild(AlertsPanel);
  private readonly gatewayPanel = viewChild(GatewayPanel);
  private readonly reconcilePanel = viewChild(ReconcilePanel);
  protected readonly auto = autoRefresh(() => [
    this.report,
    this.version,
    this.failedIngest,
    this.failedTicks,
  ]);

  protected readonly refreshing = computed(
    () => this.report.isLoading() || this.failedIngest.isLoading() || this.failedTicks.isLoading(),
  );

  protected readonly levelTone = LEVEL_TONE;
  protected readonly levelLabel = LEVEL_LABEL;
  protected readonly levelUrgency = LEVEL_URGENCY;
  protected readonly checkTitle = checkTitle;
  protected readonly checkMeaning = checkMeaning;
  protected readonly checkPill = checkPill;
  protected readonly plainDetail = plainDetail;
  protected readonly runWord = runWord;
  protected readonly freshnessMeaning = checkMeaning('freshness');
  protected checkAction(name: string) {
    return CHECK_ACTIONS[name] ?? null;
  }

  private readonly split = computed(() =>
    this.report.hasValue() ? splitChecks(this.report.value().checks) : { freshness: [], other: [] },
  );
  protected readonly freshness = computed(() => this.split().freshness);
  protected readonly runChecks = computed(() => this.split().other);

  protected readonly level = computed<HealthLevel>(() =>
    this.report.hasValue() ? worstLevel(this.report.value().checks.map(checkLevel)) : 'good',
  );

  protected readonly counts = computed(() => {
    const checks = this.report.hasValue() ? this.report.value().checks : [];
    const levels = checks.map(checkLevel);
    return {
      total: levels.length,
      critical: levels.filter((l) => l === 'critical').length,
      warning: levels.filter((l) => l === 'warning').length,
    };
  });

  protected readonly headline = computed(() => {
    const { total, critical, warning } = this.counts();
    if (total === 0) return 'No checks ran';
    if (!critical && !warning)
      return total === 1 ? 'The check passed' : `All ${total} checks passed`;
    const failed = critical + warning;
    return `${failed} of ${total} ${total === 1 ? 'check' : 'checks'} failed`;
  });

  protected readonly advice = computed(() => {
    switch (this.level()) {
      case 'good':
        return 'Data is fresh and no trading runs or data updates are stuck or failing.';
      case 'warning':
        return 'Something needs a look soon: stale data or a recent data update failure. Details below.';
      default:
        return 'Act now: data is missing or far out of date, a run is stuck, or a check could not run.';
    }
  });

  async runChecksNow(): Promise<void> {
    if (this.runningChecks() || !this.canRunChecks()) return;
    const ok = await this.confirm.confirm({
      title: 'Run the health checks now?',
      message:
        'Runs every check now, like the scheduled health job. Stale data or a stuck run stops trading for everyone until the checks pass again, and passing checks lift that stop.',
      confirmLabel: 'Run checks now',
      tone: 'danger',
    });
    if (!ok) return;
    this.runningChecks.set(true);
    try {
      const result = await this.healthApi.runChecks(this.tickers());
      this.toasts.success(
        result.healthy ? 'Ran the health checks: all pass.' : 'Ran the health checks: some fail.',
      );
      this.report.reload();
      void this.halts.refresh();
    } catch {
      // The error interceptor already showed the API's message.
    } finally {
      this.runningChecks.set(false);
    }
  }

  protected readonly checkedAt = computed(() => {
    if (!this.report.hasValue()) return 'Freshness, stuck runs and recent failures.';
    const at = this.report.value().checked_at;
    return `Checked ${formatDateTime(at)} (${formatAgo(at)}).`;
  });

  protected readonly freshSummary = computed(() => {
    const rows = this.freshness();
    const fresh = rows.filter((r) => r.level === 'good').length;
    return rows.length ? `${fresh} of ${rows.length}` : '–';
  });

  protected readonly freshDetail = computed(() => {
    const rows = this.freshness();
    if (!rows.length) return 'No tickers checked';
    const stale = rows.length - rows.filter((r) => r.level === 'good').length;
    return stale ? `${stale} stale or missing` : 'All tickers fresh';
  });

  protected readonly freshTone = computed<StatTone>(() => {
    const rows = this.freshness();
    if (!rows.length) return '';
    return rows.every((r) => r.level === 'good') ? 'gain' : 'loss';
  });

  protected readonly stuckCount = computed(
    () =>
      this.runChecks().filter(
        (c) => (c.name === 'stuck_ticks' || c.name === 'stuck_ingest_runs') && !c.ok,
      ).length,
  );

  /** The limit a check compares against, from the report's thresholds. */
  protected threshold(name: string): string | null {
    return checkThreshold(name, this.report.hasValue() ? this.report.value().thresholds : null);
  }

  protected readonly freshnessColumns: TableColumn<FreshnessRow>[] = [
    { key: 'ticker', label: 'Ticker', mobile: 'title' },
    {
      key: 'level',
      label: 'State',
      value: (r) => ({ good: 0, warning: 1, critical: 2 })[r.level],
    },
    { key: 'detail', label: 'Latest price', sortable: false },
  ];
  protected readonly freshnessKey = (r: FreshnessRow) => r.ticker;

  protected readonly ingestColumns: TableColumn<IngestRunView>[] = [
    { key: 'started_at', label: 'Started', format: 'datetime', mobile: 'title' },
    { key: 'source', label: 'Source', value: (r) => sourceLabel(r.source), mobile: 'hide' },
    { key: 'kind', label: 'Update', value: (r) => kindLabel(r.kind) },
    {
      key: 'tickers',
      label: 'Tickers updated / failed',
      sortable: false,
      align: 'end',
      value: (r) => `${r.tickers_ok ?? 0} / ${r.tickers_failed ?? 0}`,
      mobile: 'hide',
    },
    { key: 'error', label: 'Error', sortable: false },
  ];
  protected readonly ingestKey = (r: IngestRunView) => String(r.id);

  protected readonly tickColumns: TableColumn<TickRun>[] = [
    { key: 'started_at', label: 'Started', format: 'datetime', mobile: 'title' },
    { key: 'status', label: 'Status' },
    {
      key: 'error',
      label: 'Error',
      sortable: false,
      value: (t) => t.summary?.error ?? t.summary?.reason ?? null,
    },
  ];
  protected readonly tickKey = (t: TickRun) => t.id;
  protected readonly dateTime = formatDateTime;

  protected applyTickers(text: string): void {
    this.tickers.set(parseTickers(text));
  }

  protected resetTickers(input: HTMLInputElement): void {
    input.value = '';
    this.tickers.set([]);
  }

  /** Every panel, the system alerts included. */
  protected refresh(): void {
    this.auto.refresh();
    this.alertsPanel()?.reload();
    this.gatewayPanel()?.reload();
    this.reconcilePanel()?.reload();
  }
}
