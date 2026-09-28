import { ChangeDetectionStrategy, Component, inject, linkedSignal, resource } from '@angular/core';
import { RouterLink } from '@angular/router';

import type { JournalEntryView } from '../../api/models';
import { TcaService } from '../../api/tca.service';
import { PortfolioContextService } from '../../core/portfolio/portfolio-context.service';
import { DataTable, TableCell, type TableColumn } from '../../shared/ui/data-table/data-table';
import { keepLatest } from '../../shared/ui/data-table/keep-latest';
import { SideTag } from '../../shared/ui/side-tag';
import { EmptyState, ErrorState, LoadingState } from '../../shared/ui/states';
import { StatusPill } from '../../shared/ui/status-pill';
import { bps1 } from './trades-format';

export const JOURNAL_PAGE_SIZE = 25;

/** The trade journal: every order, newest first, with its cost and notes. */
@Component({
  selector: 'app-trade-journal',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    RouterLink,
    DataTable,
    TableCell,
    SideTag,
    StatusPill,
    EmptyState,
    ErrorState,
    LoadingState,
  ],
  styleUrl: './trades.scss',
  template: `
    <section class="panel" aria-labelledby="journal-title">
      <div class="panel-head">
        <h2 id="journal-title">Trade journal</h2>
        @if (latest(); as p) {
          @if (p.total > 0) {
            <span class="muted num">{{ p.total }} {{ p.total === 1 ? 'order' : 'orders' }}</span>
          }
        }
      </div>

      @let page = latest();
      @if (journal.error(); as err) {
        <app-error-state
          title="Could not load the journal"
          [error]="err"
          (retry)="journal.reload()"
        />
      } @else if (!page) {
        <app-loading-state label="Loading the journal" [rows]="6" />
      } @else if (page.items.length === 0) {
        <app-empty-state title="No trades yet" message="Costs appear after the first filled order.">
          <a class="btn" routerLink="/orders">See orders</a>
        </app-empty-state>
      } @else {
        <app-data-table
          caption="Trade journal, newest first. Open an order for its costs and notes."
          [rows]="page.items"
          [columns]="columns"
          [rowKey]="rowKey"
          [total]="page.total"
          [offset]="page.offset"
          [pageSize]="pageSize"
          [busy]="journal.isLoading()"
          (pageChange)="offset.set($event.offset)"
        >
          <ng-template appCell="ticker" [appCellOf]="page.items" let-o>
            <span class="ticker-cell">
              <app-side-tag [side]="o.side" />
              <a [routerLink]="['/trades/orders', o.client_id]">{{ o.ticker }}</a>
            </span>
          </ng-template>
          <ng-template appCell="status" [appCellOf]="page.items" let-o>
            <app-status-pill [status]="o.status" />
          </ng-template>
        </app-data-table>
      }
    </section>
  `,
})
export class TradeJournal {
  private readonly tca = inject(TcaService);
  private readonly ctx = inject(PortfolioContextService);

  protected readonly pageSize = JOURNAL_PAGE_SIZE;
  /** Back to the first page when the portfolio changes. */
  protected readonly offset = linkedSignal({ source: this.ctx.selectedId, computation: () => 0 });

  protected readonly journal = resource({
    params: () => ({ offset: this.offset(), portfolio: this.ctx.selectedId() }),
    loader: ({ params }) => this.tca.journal({ limit: JOURNAL_PAGE_SIZE, offset: params.offset }),
  });
  /** The last page stays on screen, dimmed, while the next loads (UX-35). */
  protected readonly latest = keepLatest(this.journal);

  protected readonly columns: TableColumn<JournalEntryView>[] = [
    { key: 'ticker', label: 'Order', mobile: 'title' },
    { key: 'quantity', label: 'Qty', format: 'number' },
    { key: 'decision_price', label: 'Decided at', format: 'money' },
    {
      key: 'fill_price',
      label: 'Filled at',
      format: 'money',
      value: (o) => o.shortfall?.fill_price ?? null,
    },
    {
      key: 'cost_bps',
      label: 'Cost (bps)',
      format: 'number',
      value: (o) => bps1(o.shortfall?.is_bps),
    },
    {
      key: 'cost',
      label: 'Cost',
      format: 'money',
      mobile: 'hide',
      value: (o) => o.shortfall?.is_cost ?? null,
    },
    { key: 'notes', label: 'Notes', format: 'number', value: (o) => o.notes.length },
    { key: 'status', label: 'Status', mobile: 'hide' },
    {
      key: 'decided_at',
      label: 'Decided',
      format: 'datetime',
      mobile: 'hide',
      value: (o) => o.decided_at ?? o.created_at,
    },
  ];

  protected readonly rowKey = (o: JournalEntryView) => o.client_id;
}
