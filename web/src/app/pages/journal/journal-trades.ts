import {
  ChangeDetectionStrategy,
  Component,
  inject,
  linkedSignal,
  resource,
  signal,
} from '@angular/core';
import { RouterLink } from '@angular/router';

import { JournalService } from '../../api/journal.service';
import type { JournalTradeView } from '../../api/models';
import { PortfolioContextService } from '../../core/portfolio/portfolio-context.service';
import { DataTable, TableCell, type TableColumn } from '../../shared/ui/data-table/data-table';
import { keepLatest } from '../../shared/ui/data-table/keep-latest';
import { Segmented, type SegmentOption } from '../../shared/ui/segmented';
import { EmptyState, ErrorState, LoadingState } from '../../shared/ui/states';
import {
  exitLabel,
  formatHolding,
  formatR,
  formatShare,
  planLabel,
  sleeveLabel,
} from './journal-format';

export const TRADES_PAGE_SIZE = 25;

type Status = 'all' | 'open' | 'closed';

const STATUSES: readonly SegmentOption<Status>[] = [
  { value: 'all', label: 'All' },
  { value: 'open', label: 'Open' },
  { value: 'closed', label: 'Closed' },
];

/** Every round trip of the picked portfolio: open first, then newest exit first. */
@Component({
  selector: 'app-journal-trades',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink, DataTable, TableCell, Segmented, EmptyState, ErrorState, LoadingState],
  styleUrl: './journal.scss',
  template: `
    <section class="panel" aria-labelledby="trips-title">
      <div class="panel-head">
        <h2 id="trips-title">Trades</h2>
        <app-segmented label="Show trades" [options]="statuses" [(value)]="status" />
      </div>
      <p class="lead">
        Each trade runs from the fill that opened it to the fill that closed it. A partial exit is
        its own line. Excursions are the worst and best moves while you held. R is the result in
        units of the risk at your first stop.
      </p>

      @let page = latest();
      @if (trades.error(); as err) {
        <app-error-state
          title="Could not load your trades"
          [error]="err"
          (retry)="trades.reload()"
        />
      } @else if (!page) {
        <app-loading-state label="Loading your trades" [rows]="6" />
      } @else if (page.items.length === 0) {
        <app-empty-state
          title="No trades yet"
          message="Trades show here once an order fills in this portfolio."
        >
          <a class="btn" routerLink="/orders">See orders</a>
        </app-empty-state>
      } @else {
        <app-data-table
          caption="Round trips. Open a trade to review it."
          [rows]="page.items"
          [columns]="columns"
          [rowKey]="rowKey"
          [total]="page.total"
          [offset]="page.offset"
          [pageSize]="pageSize"
          [busy]="trades.isLoading()"
          (pageChange)="offset.set($event.offset)"
        >
          <ng-template appCell="ticker" [appCellOf]="page.items" let-t>
            <a class="trade-link" [routerLink]="['/journal/trades', t.trade_id]"
              >{{ t.ticker }}
              <span class="muted">{{ t.side === 'short' ? 'short' : 'long' }}</span></a
            >
          </ng-template>
        </app-data-table>
      }
    </section>
  `,
})
export class JournalTrades {
  private readonly journal = inject(JournalService);
  private readonly ctx = inject(PortfolioContextService);

  protected readonly statuses = STATUSES;
  protected readonly pageSize = TRADES_PAGE_SIZE;
  readonly status = signal<Status>('all');
  /** Back to the first page when the portfolio or the filter changes. */
  protected readonly offset = linkedSignal({
    source: () => [this.ctx.selectedId(), this.status()],
    computation: () => 0,
  });

  protected readonly trades = resource({
    params: () => ({
      offset: this.offset(),
      status: this.status(),
      portfolio: this.ctx.selectedId(),
    }),
    loader: ({ params }) =>
      this.journal.trades({
        status: params.status,
        limit: TRADES_PAGE_SIZE,
        offset: params.offset,
      }),
  });
  protected readonly latest = keepLatest(this.trades);

  protected readonly columns: TableColumn<JournalTradeView>[] = [
    { key: 'ticker', label: 'Trade', mobile: 'title' },
    {
      key: 'sleeve',
      label: 'Strategy',
      value: (t) => sleeveLabel(t.sleeve, t.sleeve_name),
    },
    { key: 'entry_at', label: 'Entered', format: 'date', mobile: 'hide' },
    {
      key: 'holding_days',
      label: 'Held',
      value: (t) => t.holding_days,
      display: (t) => formatHolding(t.holding_days),
    },
    {
      key: 'pnl',
      label: 'P&L',
      format: 'signedMoney',
      tone: true,
      currency: (t) => t.currency,
    },
    {
      key: 'r_multiple',
      label: 'R',
      format: 'number',
      tone: true,
      display: (t) => formatR(t.r_multiple),
    },
    {
      key: 'mae_pct',
      label: 'Worst move',
      format: 'number',
      mobile: 'hide',
      display: (t) => formatShare(t.mae_pct, true),
    },
    {
      key: 'mfe_pct',
      label: 'Best move',
      format: 'number',
      mobile: 'hide',
      display: (t) => formatShare(t.mfe_pct, true),
    },
    {
      key: 'exit_efficiency',
      label: 'Exit efficiency',
      format: 'number',
      mobile: 'hide',
      display: (t) => formatShare(t.exit_efficiency),
    },
    {
      key: 'exit',
      label: 'Exit',
      value: (t) => exitLabel(t.exit_trigger, t.is_open),
    },
    {
      key: 'plan',
      label: 'Plan',
      mobile: 'hide',
      value: (t) => planLabel(t.followed_plan),
    },
    {
      key: 'tags',
      label: 'Tags',
      mobile: 'hide',
      sortable: false,
      value: (t) => [...t.tags, ...t.mistakes].join(', '),
    },
  ];

  protected readonly rowKey = (t: JournalTradeView) => t.leg_id;
}
