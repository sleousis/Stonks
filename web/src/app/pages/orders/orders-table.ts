import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';
import { RouterLink } from '@angular/router';

import type { OrderView } from '../../api/models';
import {
  DataTable,
  type PageRequest,
  TableCell,
  type TableColumn,
} from '../../shared/ui/data-table/data-table';
import { SideTag } from '../../shared/ui/side-tag';
import { OrderStatus, orderReason } from './order-status';

/**
 * Orders as a table (cards on phones): used by the orders list and by the
 * trading run drill-down. The ticker opens the order's detail (costs and
 * fills); the run column links to the run; status shows the rejection reason
 * when the ledger has one. Client ids stay on the detail page.
 */
@Component({
  selector: 'app-orders-table',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink, DataTable, TableCell, OrderStatus, SideTag],
  template: `
    <app-data-table
      [caption]="caption()"
      [rows]="rows()"
      [columns]="columns()"
      [rowKey]="key"
      [total]="total()"
      [offset]="offset()"
      [pageSize]="pageSize()"
      [busy]="busy()"
      [initialSort]="{ key: 'created_at', dir: 'desc' }"
      (pageChange)="pageChange.emit($event)"
    >
      <ng-template appCell="ticker" [appCellOf]="rows()" let-o>
        <a
          class="cell-link"
          [routerLink]="['/trades/orders', o.client_id]"
          [attr.aria-label]="'Order details: ' + o.side + ' ' + o.quantity + ' ' + o.ticker"
          >{{ o.ticker }}</a
        >
      </ng-template>
      <ng-template appCell="side" [appCellOf]="rows()" let-o>
        <app-side-tag [side]="o.side" />
      </ng-template>
      <ng-template appCell="status" [appCellOf]="rows()" let-o>
        <app-order-status [status]="o.status" [reason]="reason(o)" />
      </ng-template>
      <ng-template appCell="tick_id" [appCellOf]="rows()" let-o>
        @if (o.tick_id) {
          <a class="cell-link" [routerLink]="['/orders/ticks', o.tick_id]">View run</a>
        } @else {
          <span class="muted">–</span>
        }
      </ng-template>
    </app-data-table>
  `,
  styleUrl: './orders-views.scss',
})
export class OrdersTable {
  readonly rows = input.required<readonly OrderView[]>();
  readonly caption = input('Orders placed by trading runs');
  /** Server paging: the page's total. Null pages on the client. */
  readonly total = input<number | null>(null);
  readonly offset = input<number | null>(null);
  readonly pageSize = input(50);
  /** The next server page is loading; the current rows stay, dimmed. */
  readonly busy = input(false);
  /** Off inside a tick's own drill-down, where the link would point to itself. */
  readonly linkTicks = input(true);
  readonly pageChange = output<PageRequest>();

  private readonly allColumns: TableColumn<OrderView>[] = [
    { key: 'ticker', label: 'Ticker', mobile: 'title' },
    { key: 'side', label: 'Side' },
    { key: 'quantity', label: 'Qty', format: 'number' },
    { key: 'limit_price', label: 'Limit', format: 'money', mobile: 'hide' },
    { key: 'status', label: 'Status' },
    { key: 'strategy_id', label: 'Strategy' },
    { key: 'tick_id', label: 'Run', sortable: false, mobile: 'hide' },
    { key: 'created_at', label: 'Created', format: 'datetime', mobile: 'hide' },
  ];
  /** Inside a run's own drill-down the run column would point to itself. */
  protected readonly columns = computed(() =>
    this.linkTicks() ? this.allColumns : this.allColumns.filter((c) => c.key !== 'tick_id'),
  );
  protected readonly key = (o: OrderView) => o.client_id;
  protected readonly reason = orderReason;
}
