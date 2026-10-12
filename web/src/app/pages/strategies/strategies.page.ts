import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  input,
  linkedSignal,
  resource,
} from '@angular/core';
import { ActivatedRoute, Router, RouterLink } from '@angular/router';

import type { ShadowPnlSummary, StrategyStatus, StrategySummary } from '../../api/models';
import { ShadowService } from '../../api/shadow.service';
import { StrategiesService } from '../../api/strategies.service';
import { SubscriptionsService, type SubscriptionView } from '../../api/subscriptions.service';
import { formatPercent } from '../../core/format/format';
import { PortfolioContextService } from '../../core/portfolio/portfolio-context.service';
import { STATUS_WORDS, modeLabel } from '../../shared/governance-labels';
import { DataTable, TableCell, type TableColumn } from '../../shared/ui/data-table/data-table';
import { ModeStamp } from '../../shared/ui/mode-stamp';
import { PageHeader } from '../../shared/ui/page-header';
import { StatTile } from '../../shared/ui/stat-tile';
import { EmptyState, ErrorState, LoadingState } from '../../shared/ui/states';
import { StatusPill } from '../../shared/ui/status-pill';
import { DaysRing } from './days-ring';
import { Sparkline } from './sparkline';
import { STATUS_FILTERS, asStatus, strategyDisplayName, strategyKindName } from './strategy-format';

/** Enough for any realistic registry; filters, search and paging then run in the browser. */
const FETCH_LIMIT = 500;
/** Trend lines drawn at most: one small read per strategy with a test book. */
export const SPARK_MAX = 40;
/** Days the trend line covers. */
export const SPARK_DAYS = 60;

/** How you follow a strategy, for the Following column. */
export interface Following {
  /** "Alerts only", "Paper", "Approve each trade", "Automatic" or "Paused". */
  words: string;
  /** null: no stamp (alerts only, paused); else whether real money moves. */
  live: boolean | null;
}

/** A registry row with its trial result, when it has a test book. */
export interface StrategyRow extends StrategySummary {
  /** The name to show (UX-27): a starter's title, else from the id. */
  name: string;
  /** A starter strategy's one-line summary. */
  summary: string | null;
  kind: string;
  paper_return: number | null;
  max_drawdown: number | null;
  paper_days: number | null;
  /** The last 60 days of its test book value, for the trend line. */
  curve: number[] | null;
  following: Following | null;
}

/** Join the registry with the paper P&L summaries by strategy id. */
export function withPerformance(
  items: readonly StrategySummary[],
  paper: readonly ShadowPnlSummary[],
): StrategyRow[] {
  const byId = new Map(paper.map((p) => [p.strategy_id, p]));
  return items.map((s) => {
    const p = byId.get(s.id);
    return {
      ...s,
      name: strategyDisplayName(s.id, { starter: s.starter, name: s.name }),
      summary: s.starter?.summary ?? null,
      kind: s.kind || strategyKindName(s.class_path),
      paper_return: p?.cumulative_return ?? null,
      max_drawdown: p?.max_drawdown ?? null,
      paper_days: p ? p.days : null,
      curve: null,
      following: null,
    };
  });
}

/**
 * How you follow each strategy: the subscription in the picked portfolio
 * first, else any. Automatic and Approve each trade carry the LIVE stamp
 * only when that portfolio trades real money.
 */
export function followingByStrategy(
  subs: readonly SubscriptionView[],
  pickedId: string | null,
  isLive: (portfolioId: string | null) => boolean,
): Map<string, Following> {
  const best = new Map<string, SubscriptionView>();
  for (const s of subs) {
    const prev = best.get(s.strategy_id);
    const picked = (x: SubscriptionView) => x.portfolio_id === pickedId;
    if (!prev || (!picked(prev) && picked(s)) || (!prev.enabled && s.enabled)) {
      best.set(s.strategy_id, s);
    }
  }
  const out = new Map<string, Following>();
  for (const [id, s] of best) {
    if (!s.enabled) out.set(id, { words: 'Paused', live: null });
    else if (s.mode === 'notify') out.set(id, { words: modeLabel('notify'), live: null });
    else if (s.mode === 'paper') out.set(id, { words: modeLabel('paper'), live: false });
    else out.set(id, { words: modeLabel(s.mode), live: isLive(s.portfolio_id) });
  }
  return out;
}

/**
 * The standfirst, from the data: "3 approved, 2 on trial. Quant value leads
 * at +8.42%." Retired strategies are counted only when nothing else is.
 */
export function strategiesStandfirst(rows: readonly StrategyRow[]): string {
  if (!rows.length) return 'No strategies yet. Build one in Studio or test one in Lab.';
  const count = (s: StrategyStatus) => rows.filter((r) => r.status === s).length;
  const parts = [
    count('active') ? `${count('active')} approved` : null,
    count('shadow') ? `${count('shadow')} on trial` : null,
  ].filter((p): p is string => p !== null);
  const head = parts.length ? parts.join(', ') : `${count('retired')} retired`;
  const leader = bestReturn(rows);
  return leader && (leader.paper_return ?? 0) > 0
    ? `${capitalise(head)}. ${leader.name} leads at ${formatPercent(leader.paper_return, { signed: true })}.`
    : `${capitalise(head)}.`;
}

/** The strategy with the best trial return, retired ones left out. */
export function bestReturn(rows: readonly StrategyRow[]): StrategyRow | null {
  return (
    rows
      .filter((r) => r.status !== 'retired' && r.paper_return !== null)
      .sort((a, b) => b.paper_return! - a.paper_return!)[0] ?? null
  );
}

/** The strategy with the worst drop on trial, retired ones left out. */
export function worstDrop(rows: readonly StrategyRow[]): StrategyRow | null {
  return (
    rows
      .filter((r) => r.status !== 'retired' && r.max_drawdown !== null)
      .sort((a, b) => a.max_drawdown! - b.max_drawdown!)[0] ?? null
  );
}

function capitalise(text: string): string {
  return text.charAt(0).toUpperCase() + text.slice(1);
}

/**
 * The Strategies page (`/strategies`), the reference table page: a summary
 * strip, filter pills with counts, a search, and one row per strategy with
 * its status, trial return, a 60-day trend line, worst drop, days on trial
 * and how you follow it. On phones each row is a list item.
 */
@Component({
  selector: 'app-strategies-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    RouterLink,
    PageHeader,
    StatTile,
    DataTable,
    TableCell,
    StatusPill,
    ModeStamp,
    Sparkline,
    DaysRing,
    LoadingState,
    EmptyState,
    ErrorState,
  ],
  templateUrl: './strategies.page.html',
  styleUrl: './strategies.page.scss',
})
export class StrategiesPage {
  private readonly strategiesApi = inject(StrategiesService);
  private readonly shadowApi = inject(ShadowService);
  private readonly subscriptionsApi = inject(SubscriptionsService);
  private readonly portfolioCtx = inject(PortfolioContextService);
  private readonly router = inject(Router);
  private readonly route = inject(ActivatedRoute);

  /** Query params (`?status=shadow&q=mom`), so filters survive links and back. */
  readonly status = input<string | undefined>();
  readonly q = input<string | undefined>();

  protected readonly statusFilter = linkedSignal<StrategyStatus | null>(() =>
    asStatus(this.status()),
  );
  protected readonly search = linkedSignal(() => this.q() ?? '');
  protected readonly filters = STATUS_FILTERS;

  /** Every strategy in one read: the pills count them and filter in the browser. */
  protected readonly strategies = resource({
    loader: () => this.strategiesApi.list({ limit: FETCH_LIMIT }),
  });

  /**
   * Paper return, drawdown and days of every paper book. Its own read, so
   * the list still shows when it fails (the columns stay empty).
   */
  protected readonly paper = resource({
    loader: () => this.shadowApi.pnlSummaries({ limit: FETCH_LIMIT }),
  });

  /** Your subscriptions, for the Following column. A failure leaves it empty. */
  protected readonly subscriptions = resource({
    loader: () => this.subscriptionsApi.list(),
  });

  /** The strategies worth a trend line: those with a test book, most days first. */
  private readonly sparkIds = computed(
    () =>
      this.paper.hasValue()
        ? [...this.paper.value().items]
            .filter((p) => p.days > 1)
            .sort((a, b) => b.days - a.days)
            .slice(0, SPARK_MAX)
            .map((p) => p.strategy_id)
        : undefined,
    { equal: (a, b) => a?.join() === b?.join() },
  );

  /** Each test book's last 60 values, settled one by one: a failed read draws no line. */
  protected readonly curves = resource({
    params: () => (this.sparkIds()?.length ? { ids: this.sparkIds()! } : undefined),
    loader: async ({ params }) => {
      const settled = await Promise.allSettled(params.ids.map((id) => this.shadowApi.pnl(id)));
      const out = new Map<string, number[]>();
      settled.forEach((r, i) => {
        if (r.status === 'fulfilled') {
          out.set(
            params.ids[i],
            r.value.rows.slice(-SPARK_DAYS).map((row) => row.total_value),
          );
        }
      });
      return out;
    },
  });

  private readonly following = computed(() => {
    if (!this.subscriptions.hasValue()) return new Map<string, Following>();
    const portfolios = this.portfolioCtx.options();
    return followingByStrategy(
      this.subscriptions.value(),
      this.portfolioCtx.selectedId(),
      (id) => portfolios.find((p) => p.id === id)?.trading === 'live',
    );
  });

  private readonly rows = computed<StrategyRow[]>(() => {
    if (!this.strategies.hasValue()) return [];
    const paper = this.paper.hasValue() ? this.paper.value().items : [];
    const curves = this.curves.hasValue() ? this.curves.value() : null;
    const following = this.following();
    return withPerformance(this.strategies.value().items, paper).map((r) => ({
      ...r,
      curve: curves?.get(r.id) ?? null,
      following: following.get(r.id) ?? null,
    }));
  });

  /** How many strategies each pill holds. */
  protected readonly counts = computed(() => {
    const rows = this.rows();
    const n = (s: StrategyStatus) => rows.filter((r) => r.status === s).length;
    return { all: rows.length, active: n('active'), shadow: n('shadow'), retired: n('retired') };
  });
  protected countOf(value: StrategyStatus | null): number {
    const c = this.counts();
    return value ? c[value] : c.all;
  }

  protected readonly visible = computed<StrategyRow[]>(() => {
    const status = this.statusFilter();
    const items = status ? this.rows().filter((r) => r.status === status) : this.rows();
    const q = this.search().trim().toLowerCase();
    if (!q) return items;
    return items.filter(
      (s) =>
        s.id.toLowerCase().includes(q) ||
        s.name.toLowerCase().includes(q) ||
        (s.summary ?? '').toLowerCase().includes(q) ||
        s.kind.toLowerCase().includes(q) ||
        s.class_path.toLowerCase().includes(q) ||
        s.applicable_asset_classes.some((a) => a.toLowerCase().includes(q)),
    );
  });

  protected readonly countLabel = computed(() => {
    if (!this.strategies.hasValue()) return null;
    const total = this.rows().length;
    const shown = this.visible().length;
    const noun = total === 1 ? 'strategy' : 'strategies';
    return shown === total ? `${total} ${noun}` : `${shown} of ${total} ${noun}`;
  });

  protected readonly standfirst = computed(() =>
    this.strategies.hasValue()
      ? strategiesStandfirst(this.rows())
      : 'Every strategy, its status and how it does on trial.',
  );

  // Summary strip --------------------------------------------------------------
  protected readonly best = computed(() => bestReturn(this.rows()));
  protected readonly worst = computed(() => worstDrop(this.rows()));
  protected readonly trialDays = computed(() => {
    const days = this.rows()
      .filter((r) => r.status === 'shadow' && r.paper_days !== null)
      .map((r) => r.paper_days!)
      .sort((a, b) => a - b);
    if (!days.length) return null;
    return days.length === 1
      ? `day ${days[0]}`
      : `day ${days[0]} to day ${days[days.length - 1]}`;
  });
  protected readonly approvedDetail = computed(() => {
    const following = this.rows().filter((r) => r.status === 'active' && r.following).length;
    return following ? `you follow ${following}` : null;
  });
  protected pct(value: number | null | undefined, signed = false): string {
    return formatPercent(value ?? null, { signed });
  }

  protected readonly columns: TableColumn<StrategyRow>[] = [
    { key: 'id', label: 'Strategy', mobile: 'title', value: (s) => s.name, help: false },
    { key: 'status', label: 'Status', mobile: 'inline', value: (s) => STATUS_WORDS[s.status] },
    {
      key: 'paper_return',
      label: 'Trial return',
      format: 'signedPercent',
      tone: true,
      mobile: 'figure',
    },
    {
      key: 'curve',
      label: 'Last 60 days',
      sortable: false,
      mobile: 'figure',
      help: false,
      value: (s) => (s.curve ? s.curve.length : null),
    },
    { key: 'max_drawdown', label: 'Worst drop', format: 'percent', tone: true },
    { key: 'paper_days', label: 'Days on trial', format: 'number' },
    {
      key: 'following',
      label: 'Following',
      mobile: 'inline',
      help: false,
      value: (s) => s.following?.words ?? null,
    },
    { key: 'created_at', label: 'Added', format: 'date', mobile: 'hide' },
    { key: 'open', label: 'Open', sortable: false, mobile: 'hide', help: false },
  ];
  protected readonly strategyKey = (s: StrategyRow) => s.id;

  /** "approved", "on trial" or "retired", for the empty filter message. */
  protected readonly filterWord = computed(() => {
    const status = this.statusFilter();
    return status ? STATUS_WORDS[status].toLowerCase() : '';
  });

  protected refresh(): void {
    this.strategies.reload();
    this.paper.reload();
    this.subscriptions.reload();
  }

  protected setStatus(value: StrategyStatus | null): void {
    this.statusFilter.set(value);
    this.syncUrl();
  }

  protected setSearch(value: string): void {
    this.search.set(value);
    this.syncUrl();
  }

  protected onSearchInput(event: Event): void {
    this.setSearch((event.target as HTMLInputElement).value);
  }

  private syncUrl(): void {
    this.router
      .navigate([], {
        relativeTo: this.route,
        queryParams: { status: this.statusFilter() ?? null, q: this.search().trim() || null },
        replaceUrl: true,
      })
      .catch(() => undefined);
  }
}
