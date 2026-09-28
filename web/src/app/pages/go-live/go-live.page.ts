import {
  ChangeDetectionStrategy,
  Component,
  computed,
  effect,
  inject,
  input,
  resource,
} from '@angular/core';
import { Router, RouterLink } from '@angular/router';

import type { LeaderboardRow } from '../../api/models';
import { StrategiesService } from '../../api/strategies.service';
import { SystemService } from '../../api/system.service';
import { brokerName, isRealMoneyBroker } from '../../shared/governance';
import { strategyDisplayName, strategyKindName } from '../../shared/strategy-names';
import { DataTable, TableCell, type TableColumn } from '../../shared/ui/data-table/data-table';
import { ModeStamp } from '../../shared/ui/mode-stamp';
import { PageHeader } from '../../shared/ui/page-header';
import { EmptyState, ErrorState, LoadingState } from '../../shared/ui/states';
import { StatusPill } from '../../shared/ui/status-pill';
import { StrategyVerdict } from '../../shared/ui/strategy-verdict';
import { goliveLabel, rowVerdict, stageLabel } from '../strategies/leaderboard.page';

const STATUS_ORDER: Record<string, number> = { shadow: 0, active: 1, retired: 2 };

/** On trial and ready first (the ones to decide), then on trial, then approved. */
export function reviewOrder(rows: readonly LeaderboardRow[]): LeaderboardRow[] {
  const ready = (r: LeaderboardRow) => (r.status === 'shadow' && r.golive_passed ? 0 : 1);
  return [...rows].sort(
    (a, b) =>
      ready(a) - ready(b) ||
      (STATUS_ORDER[a.status] ?? 3) - (STATUS_ORDER[b.status] ?? 3) ||
      a.strategy_id.localeCompare(b.strategy_id),
  );
}

/**
 * Strategy review (`/go-live`, F54): the admin's queue of strategies on
 * trial, each with its verdict and go-live check, and the broker an
 * approval would send followers' orders to. Judging and approving happen on
 * the strategy page's Review tab, so there is one place to judge a strategy
 * (F27). An old `?strategy=<id>` link opens that tab.
 */
@Component({
  selector: 'app-go-live-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    RouterLink,
    PageHeader,
    DataTable,
    TableCell,
    StatusPill,
    StrategyVerdict,
    ModeStamp,
    LoadingState,
    EmptyState,
    ErrorState,
  ],
  templateUrl: './go-live.page.html',
  styleUrl: './go-live.page.scss',
})
export class GoLivePage {
  private readonly strategiesApi = inject(StrategiesService);
  private readonly systemApi = inject(SystemService);
  private readonly router = inject(Router);

  /** Query param `?strategy=<id>` from older links: opens that strategy's Review tab. */
  readonly strategy = input<string | undefined>();

  constructor() {
    effect(() => {
      const id = this.strategy();
      if (id) {
        void this.router.navigate(['/strategies', id], {
          queryParams: { tab: 'review' },
          replaceUrl: true,
        });
      }
    });
  }

  protected readonly board = resource({
    loader: () => this.strategiesApi.leaderboard({ sort: 'return', include_retired: false }),
  });
  protected readonly broker = resource({ loader: () => this.systemApi.broker() });

  protected readonly rows = computed(() =>
    this.board.hasValue() ? reviewOrder(this.board.value().rows) : [],
  );
  protected readonly readyCount = computed(
    () => this.rows().filter((r) => r.status === 'shadow' && r.golive_passed).length,
  );
  protected readonly onTrial = computed(
    () => this.rows().filter((r) => r.status === 'shadow').length,
  );

  protected readonly columns: TableColumn<LeaderboardRow>[] = [
    { key: 'strategy_id', label: 'Strategy', mobile: 'title', sortable: false },
    { key: 'status', label: 'Status', value: (r) => stageLabel(r), sortable: false },
    { key: 'verdict', label: 'Verdict', value: (r) => rowVerdict(r).label, sortable: false },
    {
      key: 'golive',
      label: 'Go-live check',
      value: (r) => goliveLabel(r.golive_passed),
      sortable: false,
      help: false,
    },
    {
      key: 'days',
      label: 'Days on trial',
      value: (r) => r.paper.days,
      format: 'number',
      help: false,
    },
    {
      key: 'return',
      label: 'Trial return',
      value: (r) => r.paper.total_return,
      format: 'signedPercent',
      tone: true,
      mobile: 'hide',
    },
    { key: 'review', label: 'Review', sortable: false, align: 'end' },
  ];
  protected readonly rowKey = (r: LeaderboardRow) => r.strategy_id;

  protected readonly name = strategyDisplayName;
  protected readonly kind = strategyKindName;
  protected readonly verdict = rowVerdict;
  protected readonly realMoney = isRealMoneyBroker;
  protected readonly brokerName = brokerName;
}
