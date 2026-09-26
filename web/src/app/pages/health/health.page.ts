import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  resource,
  signal,
} from '@angular/core';

import { HealthService } from '../../api/health.service';
import { IngestService } from '../../api/ingest.service';
import type { IngestRunView, TickRun } from '../../api/models';
import { TicksService } from '../../api/ticks.service';
import { formatAgo, formatDateTime } from '../../core/format/format';
import { DataTable, TableCell, type TableColumn } from '../../shared/ui/data-table/data-table';
import { PageHeader } from '../../shared/ui/page-header';
import { StatTile, type StatTone } from '../../shared/ui/stat-tile';
import { EmptyState, ErrorState, LoadingState } from '../../shared/ui/states';
import { StatusPill } from '../../shared/ui/status-pill';
import { parseTickers } from '../data/ingest-request';
import {
  CHECK_TITLES,
  type FreshnessRow,
  type HealthLevel,
  LEVEL_LABEL,
  LEVEL_TONE,
  checkLevel,
  splitChecks,
  worstLevel,
} from './health-state';

const RECENT_FAILURES = 10;

@Component({
  selector: 'app-health-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    PageHeader,
    StatTile,
    StatusPill,
    DataTable,
    TableCell,
    LoadingState,
    EmptyState,
    ErrorState,
  ],
  templateUrl: './health.page.html',
  styleUrl: './health.page.scss',
})
export class HealthPage {
  private readonly healthApi = inject(HealthService);
  private readonly ingestApi = inject(IngestService);
  private readonly ticksApi = inject(TicksService);

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
    loader: () => this.ticksApi.list({ status: 'failed', limit: RECENT_FAILURES }),
  });

  protected readonly refreshing = computed(
    () => this.report.isLoading() || this.failedIngest.isLoading() || this.failedTicks.isLoading(),
  );

  protected readonly levelTone = LEVEL_TONE;
  protected readonly levelLabel = LEVEL_LABEL;

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
    if (!critical && !warning) return total === 1 ? 'The check passes' : `All ${total} checks pass`;
    const parts: string[] = [];
    if (critical) parts.push(`${critical} critical`);
    if (warning) parts.push(`${warning} ${warning === 1 ? 'warning' : 'warnings'}`);
    return parts.join(', ');
  });

  protected readonly advice = computed(() => {
    switch (this.level()) {
      case 'good':
        return 'Data is fresh and no ticks or ingest runs are stuck or failing.';
      case 'warning':
        return 'Something needs a look soon: stale data or a recent ingest failure. Details below.';
      default:
        return 'Act now: data is missing or far out of date, a run is stuck, or a check could not run.';
    }
  });

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

  protected readonly checkTitle = (name: string) => CHECK_TITLES[name] ?? name;

  protected readonly freshnessColumns: TableColumn<FreshnessRow>[] = [
    { key: 'ticker', label: 'Ticker', mobile: 'title' },
    {
      key: 'level',
      label: 'State',
      value: (r) => ({ good: 0, warning: 1, critical: 2 })[r.level],
    },
    { key: 'detail', label: 'Latest bar', sortable: false },
  ];
  protected readonly freshnessKey = (r: FreshnessRow) => r.ticker;

  protected readonly ingestColumns: TableColumn<IngestRunView>[] = [
    { key: 'id', label: 'Run', mobile: 'title', value: (r) => `#${r.id}` },
    { key: 'started_at', label: 'Started', format: 'datetime' },
    { key: 'source', label: 'Source', mobile: 'hide' },
    { key: 'kind', label: 'Kind' },
    {
      key: 'tickers',
      label: 'Tickers ok / failed',
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
    { key: 'id', label: 'Tick', mobile: 'hide' },
  ];
  protected readonly tickKey = (t: TickRun) => t.id;

  protected applyTickers(text: string): void {
    this.tickers.set(parseTickers(text));
  }

  protected resetTickers(input: HTMLInputElement): void {
    input.value = '';
    this.tickers.set([]);
  }

  protected refresh(): void {
    this.report.reload();
    this.version.reload();
    this.failedIngest.reload();
    this.failedTicks.reload();
  }
}
