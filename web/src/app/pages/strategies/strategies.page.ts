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
import { DataTable, TableCell, type TableColumn } from '../../shared/ui/data-table/data-table';
import { PageHeader } from '../../shared/ui/page-header';
import { EmptyState, ErrorState, LoadingState } from '../../shared/ui/states';
import { StatusPill } from '../../shared/ui/status-pill';
import { STATUS_WORDS } from '../../shared/governance-labels';
import { STATUS_FILTERS, asStatus, strategyDisplayName, strategyKindName } from './strategy-format';

/** Enough for any realistic registry; search and paging then run in the browser. */
const FETCH_LIMIT = 500;

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
      name: strategyDisplayName(s.id, { starter: s.starter }),
      summary: s.starter?.summary ?? null,
      kind: strategyKindName(s.class_path),
      paper_return: p?.cumulative_return ?? null,
      max_drawdown: p?.max_drawdown ?? null,
      paper_days: p ? p.days : null,
    };
  });
}

@Component({
  selector: 'app-strategies-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    RouterLink,
    PageHeader,
    DataTable,
    TableCell,
    StatusPill,
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

  protected readonly strategies = resource({
    params: () => ({ status: this.statusFilter() }),
    loader: ({ params }) =>
      this.strategiesApi.list({ status: params.status ?? undefined, limit: FETCH_LIMIT }),
  });

  /**
   * Paper return, drawdown and days of every paper book. Its own read, so
   * the list still shows when it fails (the columns stay empty).
   */
  protected readonly paper = resource({
    loader: () => this.shadowApi.pnlSummaries({ limit: FETCH_LIMIT }),
  });

  private readonly rows = computed<StrategyRow[]>(() => {
    if (!this.strategies.hasValue()) return [];
    const paper = this.paper.hasValue() ? this.paper.value().items : [];
    return withPerformance(this.strategies.value().items, paper);
  });

  protected readonly visible = computed<StrategyRow[]>(() => {
    const items = this.rows();
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
    const total = this.strategies.value().total;
    const shown = this.visible().length;
    const noun = total === 1 ? 'strategy' : 'strategies';
    return shown === total ? `${total} ${noun}` : `${shown} of ${total} ${noun}`;
  });

  protected readonly columns: TableColumn<StrategyRow>[] = [
    { key: 'id', label: 'Strategy', mobile: 'title', value: (s) => s.name },
    { key: 'status', label: 'Status' },
    { key: 'paper_return', label: 'Trial return', format: 'signedPercent', tone: true },
    { key: 'max_drawdown', label: 'Max drawdown', format: 'percent' },
    { key: 'paper_days', label: 'Days on trial', format: 'number' },
    { key: 'kind', label: 'Kind', mobile: 'hide' },
    {
      key: 'assets',
      label: 'Asset classes',
      value: (s) => s.applicable_asset_classes.join(', '),
      mobile: 'hide',
    },
    { key: 'created_at', label: 'Added', format: 'date', mobile: 'hide' },
  ];
  protected readonly strategyKey = (s: StrategyRow) => s.id;

  /** "approved", "on trial" or "retired", for the empty filter message. */
  protected readonly filterWord = computed(() => {
    const status = this.statusFilter();
    return status ? STATUS_WORDS[status].toLowerCase() : '';
  });

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
