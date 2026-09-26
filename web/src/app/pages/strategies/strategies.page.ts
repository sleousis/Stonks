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

import type { StrategyStatus, StrategySummary } from '../../api/models';
import { StrategiesService } from '../../api/strategies.service';
import { DataTable, TableCell, type TableColumn } from '../../shared/ui/data-table/data-table';
import { PageHeader } from '../../shared/ui/page-header';
import { EmptyState, ErrorState, LoadingState } from '../../shared/ui/states';
import { StatusPill } from '../../shared/ui/status-pill';
import { STATUS_FILTERS, asStatus, shortClassName } from './strategy-format';

/** Enough for any realistic registry; search and paging then run in the browser. */
const FETCH_LIMIT = 500;

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

  protected readonly visible = computed<StrategySummary[]>(() => {
    if (!this.strategies.hasValue()) return [];
    const q = this.search().trim().toLowerCase();
    const items = this.strategies.value().items;
    if (!q) return items;
    return items.filter(
      (s) =>
        s.id.toLowerCase().includes(q) ||
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

  protected readonly columns: TableColumn<StrategySummary>[] = [
    { key: 'id', label: 'Strategy', mobile: 'title' },
    { key: 'status', label: 'Status' },
    { key: 'class', label: 'Class', value: (s) => shortClassName(s.class_path) },
    {
      key: 'assets',
      label: 'Asset classes',
      value: (s) => s.applicable_asset_classes.join(', '),
    },
    { key: 'created_at', label: 'Registered', format: 'date' },
    { key: 'updated_at', label: 'Updated', format: 'datetime', mobile: 'hide' },
  ];
  protected readonly strategyKey = (s: StrategySummary) => s.id;

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
