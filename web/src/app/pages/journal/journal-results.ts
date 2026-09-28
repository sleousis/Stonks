import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  resource,
  signal,
} from '@angular/core';

import { JournalService } from '../../api/journal.service';
import type { GroupStatsView } from '../../api/models';
import { PortfolioContextService } from '../../core/portfolio/portfolio-context.service';
import { DataTable, type TableColumn } from '../../shared/ui/data-table/data-table';
import { Segmented, type SegmentOption } from '../../shared/ui/segmented';
import { EmptyState, ErrorState, LoadingState } from '../../shared/ui/states';
import { formatR, formatShare, groupLabel } from './journal-format';

export type ResultsBy = 'plan' | 'tag' | 'mistake' | 'playbook' | 'sleeve' | 'exit_trigger';

const GROUPINGS: readonly SegmentOption<ResultsBy>[] = [
  { value: 'plan', label: 'Plan' },
  { value: 'tag', label: 'Tag' },
  { value: 'mistake', label: 'Mistake' },
  { value: 'playbook', label: 'Playbook' },
  { value: 'sleeve', label: 'Strategy' },
  { value: 'exit_trigger', label: 'Exit' },
];

/** Closed trades grouped by plan, tag, mistake, playbook, strategy or exit. */
@Component({
  selector: 'app-journal-results',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [DataTable, Segmented, EmptyState, ErrorState, LoadingState],
  styleUrl: './journal.scss',
  template: `
    <section class="panel" aria-labelledby="results-title">
      <div class="panel-head">
        <h2 id="results-title">Results</h2>
        <app-segmented label="Group results by" [options]="groupings" [(value)]="by" />
      </div>
      <p class="lead">
        How your closed trades did, split by what you wrote about them. A trade with two tags counts
        once under each. Money is in the portfolio's base currency.
      </p>

      @if (results.error(); as err) {
        <app-error-state
          title="Could not load your results"
          [error]="err"
          (retry)="results.reload()"
        />
      } @else if (!results.hasValue()) {
        <app-loading-state label="Loading your results" [rows]="4" />
      } @else if (results.value().groups.length === 0) {
        <app-empty-state title="No trades yet" message="Results show once a trade closes." />
      } @else {
        <app-data-table
          [caption]="'Results by ' + byLabel().toLowerCase()"
          [rows]="results.value().groups"
          [columns]="columns()"
          [rowKey]="rowKey"
          [initialSort]="{ key: 'trades', dir: 'desc' }"
        />
      }
    </section>
  `,
})
export class JournalResults {
  private readonly journal = inject(JournalService);
  private readonly ctx = inject(PortfolioContextService);

  protected readonly groupings = GROUPINGS;
  readonly by = signal<ResultsBy>('plan');

  protected readonly results = resource({
    params: () => ({ by: this.by(), portfolio: this.ctx.selectedId() }),
    loader: ({ params }) => this.journal.breakdown({ by: params.by }),
  });

  protected readonly byLabel = computed(
    () => GROUPINGS.find((g) => g.value === this.by())?.label ?? '',
  );

  protected readonly columns = computed<TableColumn<GroupStatsView>[]>(() => {
    const by = this.by();
    const base = this.results.hasValue() ? this.results.value().base_currency : 'USD';
    return [
      {
        key: 'key',
        label: this.byLabel(),
        mobile: 'title',
        value: (g) => groupLabel(g.key, by),
      },
      { key: 'trades', label: 'Closed', format: 'number' },
      { key: 'win_rate', label: 'Win rate', format: 'percent' },
      { key: 'pnl', label: 'P&L', format: 'signedMoney', tone: true, currency: () => base },
      {
        key: 'avg_r',
        label: 'Average R',
        format: 'number',
        tone: true,
        display: (g) => formatR(g.avg_r),
      },
      {
        key: 'profit_factor',
        label: 'Profit factor',
        format: 'number',
        mobile: 'hide',
      },
      {
        key: 'avg_exit_efficiency',
        label: 'Exit efficiency',
        format: 'number',
        mobile: 'hide',
        display: (g) => formatShare(g.avg_exit_efficiency),
      },
      { key: 'open', label: 'Open', format: 'number', mobile: 'hide' },
    ];
  });

  protected readonly rowKey = (g: GroupStatsView) => g.key;
}
