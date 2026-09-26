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

import type { StrategySummary } from '../../api/models';
import { StrategiesService } from '../../api/strategies.service';
import { SystemService } from '../../api/system.service';
import { formatMoney, formatPercent } from '../../core/format/format';
import { CliCommand } from '../../shared/ui/cli-command';
import { PageHeader } from '../../shared/ui/page-header';
import { EmptyState, ErrorState, LoadingState } from '../../shared/ui/states';
import { StatusPill } from '../../shared/ui/status-pill';

/** What `stonks golive check` evaluates (src/stonks/production/golive.py). */
export const GATE_CHECKS: readonly { name: string; measures: string }[] = [
  { name: 'status', measures: 'The strategy is in shadow or active, so it has a paper period.' },
  { name: 'min_days', measures: 'Distinct days with a paper snapshot.' },
  { name: 'max_drawdown', measures: 'Deepest peak-to-trough fall during the paper period.' },
  {
    name: 'max_drift',
    measures: 'Gap between the paper return and the return its out-of-sample backtest expects.',
  },
  { name: 'min_trades', measures: 'Filled trades during the paper period.' },
  { name: 'survival', measures: 'Every stored survival report passed.' },
];

const STATUS_ORDER: Record<StrategySummary['status'], number> = {
  shadow: 0,
  active: 1,
  retired: 2,
};

@Component({
  selector: 'app-go-live-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink, PageHeader, StatusPill, CliCommand, LoadingState, EmptyState, ErrorState],
  templateUrl: './go-live.page.html',
  styleUrl: './go-live.page.scss',
})
export class GoLivePage {
  private readonly strategiesApi = inject(StrategiesService);
  private readonly systemApi = inject(SystemService);
  private readonly router = inject(Router);
  private readonly route = inject(ActivatedRoute);

  /** Query param `?strategy=<id>`, so Strategies can link straight to a check. */
  readonly strategy = input<string | undefined>();
  protected readonly selectedId = linkedSignal(() => this.strategy() ?? '');

  protected readonly strategies = resource({
    loader: () => this.strategiesApi.list({ limit: 500 }),
  });
  protected readonly broker = resource({ loader: () => this.systemApi.broker() });
  protected readonly risk = resource({ loader: () => this.systemApi.riskPolicy() });

  protected readonly checks = GATE_CHECKS;

  /** Shadow first (the usual candidates), then active, then retired. */
  protected readonly groups = computed(() => {
    if (!this.strategies.hasValue()) return [];
    const items = [...this.strategies.value().items].sort(
      (a, b) => STATUS_ORDER[a.status] - STATUS_ORDER[b.status] || a.id.localeCompare(b.id),
    );
    const labels = { shadow: 'Shadow', active: 'Active', retired: 'Retired' } as const;
    return (['shadow', 'active', 'retired'] as const)
      .map((status) => ({ label: labels[status], items: items.filter((s) => s.status === status) }))
      .filter((g) => g.items.length > 0);
  });

  protected readonly selected = computed<StrategySummary | null>(() => {
    const id = this.selectedId();
    if (!id || !this.strategies.hasValue()) return null;
    return this.strategies.value().items.find((s) => s.id === id) ?? null;
  });

  /** Selected id that is not in the registry (a stale link). */
  protected readonly unknownId = computed(() => {
    const id = this.selectedId();
    return id && this.strategies.hasValue() && !this.selected() ? id : null;
  });

  protected readonly command = computed(() => `stonks golive check ${this.selectedId()}`);

  protected readonly riskRows = computed(() => {
    if (!this.risk.hasValue()) return [];
    const r = this.risk.value();
    const rows = [
      { label: 'Enforced', value: r.enabled === false ? 'No' : 'Yes' },
      {
        label: 'Max weight per ticker',
        value: formatPercent(r.max_weight_per_ticker, { digits: 1 }),
      },
      {
        label: 'Max open positions',
        value: r.max_open_positions == null ? 'No limit' : String(r.max_open_positions),
      },
      { label: 'Cash buffer', value: formatPercent(r.cash_buffer_fraction, { digits: 1 }) },
      { label: 'Min order', value: formatMoney(r.min_order_notional) },
    ];
    for (const [cls, w] of Object.entries(r.max_weight_per_asset_class ?? {})) {
      rows.push({ label: `Max weight, ${cls}`, value: formatPercent(w, { digits: 1 }) });
    }
    return rows;
  });

  protected onSelect(event: Event): void {
    const id = (event.target as HTMLSelectElement).value;
    this.selectedId.set(id);
    this.router
      .navigate([], {
        relativeTo: this.route,
        queryParams: { strategy: id || null },
        replaceUrl: true,
      })
      .catch(() => undefined);
  }
}
