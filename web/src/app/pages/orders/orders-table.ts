import { ChangeDetectionStrategy, Component, input, output } from '@angular/core';
import { RouterLink } from '@angular/router';

import type { OrderView } from '../../api/models';
import {
  DataTable,
  type PageRequest,
  TableCell,
  type TableColumn,
} from '../../shared/ui/data-table/data-table';
import { OrderStatus, orderReason } from './order-status';

/**
 * Orders as a table (cards on phones): used by the orders list and by the
 * tick drill-down. Tick ids link to the tick; status shows the rejection
 * reason when the ledger has one.
 */
@Component({
  selector: 'app-orders-table',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink, DataTable, TableCell, OrderStatus],
  template: `
    <app-data-table
      [caption]="caption()"
      [rows]="rows()"
      [columns]="columns"
      [rowKey]="key"
      [total]="total()"
      [pageSize]="pageSize()"
      [initialSort]="{ key: 'created_at', dir: 'desc' }"
      (pageChange)="pageChange.emit($event)"
    >
      <ng-template appCell="side" [appCellOf]="rows()" let-o>
        <span class="side" [attr.data-side]="o.side">{{ o.side }}</span>
      </ng-template>
      <ng-template appCell="status" [appCellOf]="rows()" let-o>
        <app-order-status [status]="o.status" [reason]="reason(o)" />
      </ng-template>
      <ng-template appCell="tick_id" [appCellOf]="rows()" let-o>
        @if (o.tick_id && linkTicks()) {
          <a class="cell-link mono" [routerLink]="['/orders/ticks', o.tick_id]">{{ o.tick_id }}</a>
        } @else {
          <span class="mono">{{ o.tick_id ?? '–' }}</span>
        }
      </ng-template>
      <ng-template appCell="client_id" [appCellOf]="rows()" let-o>
        <span class="mono id">{{ o.client_id }}</span>
      </ng-template>
    </app-data-table>
  `,
  styleUrl: './orders-views.scss',
})
export class OrdersTable {
  readonly rows = input.required<readonly OrderView[]>();
  readonly caption = input('Orders placed by ticks');
  /** Server paging: the page's total. Null pages on the client. */
  readonly total = input<number | null>(null);
  readonly pageSize = input(50);
  /** Off inside a tick's own drill-down, where the link would point to itself. */
  readonly linkTicks = input(true);
  readonly pageChange = output<PageRequest>();

  protected readonly columns: TableColumn<OrderView>[] = [
    { key: 'ticker', label: 'Ticker', mobile: 'title' },
    { key: 'side', label: 'Side' },
    { key: 'quantity', label: 'Qty', format: 'number' },
    { key: 'status', label: 'Status' },
    { key: 'strategy_id', label: 'Strategy' },
    { key: 'tick_id', label: 'Tick', mobile: 'hide' },
    { key: 'client_id', label: 'Client id', mobile: 'hide' },
    { key: 'created_at', label: 'Created', format: 'datetime', mobile: 'hide' },
  ];
  protected readonly key = (o: OrderView) => o.client_id;
  protected readonly reason = orderReason;
}
