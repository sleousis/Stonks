import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  resource,
  signal,
  viewChild,
} from '@angular/core';
import { RouterLink } from '@angular/router';

import { ManualOrdersService } from '../../api/manual-orders.service';
import type { OrderView } from '../../api/models';
import { OrdersService } from '../../api/orders.service';
import { SessionService } from '../../core/auth/session.service';
import { StepUpService } from '../../core/auth/step-up.service';
import { formatNumber } from '../../core/format/format';
import { ToastService } from '../../core/notify/toast.service';
import { PortfolioContextService } from '../../core/portfolio/portfolio-context.service';
import { DataTable, TableCell, type TableColumn } from '../../shared/ui/data-table/data-table';
import { SideTag } from '../../shared/ui/side-tag';
import { EmptyState, ErrorState, LoadingState } from '../../shared/ui/states';
import { StatusChangeDialog } from '../../shared/ui/status-change-dialog';
import { OrderChangeSheet } from './order-change-sheet';
import { OrderStatus, orderReason } from './order-status';

/** Statuses of an order that is still working at the broker (or the book). */
export const WORKING_STATUSES: ReadonlySet<string> = new Set([
  'pending',
  'submitted',
  'partially_filled',
]);

export function isWorking(order: Pick<OrderView, 'status'>): boolean {
  return WORKING_STATUSES.has(order.status);
}

/**
 * Your latest orders placed by hand in the picked portfolio. A working one
 * can change (quantity or limit, placed again through every check) or be
 * cancelled, each with a reason. Real-money books ask for a fresh code.
 */
@Component({
  selector: 'app-manual-orders-list',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    RouterLink,
    DataTable,
    TableCell,
    SideTag,
    OrderStatus,
    LoadingState,
    EmptyState,
    ErrorState,
    StatusChangeDialog,
    OrderChangeSheet,
  ],
  template: `
    <section class="panel" aria-labelledby="manual-list-title">
      <div class="panel-head">
        <h2 id="manual-list-title">Your orders by hand</h2>
      </div>
      @if (orders.error(); as err) {
        <app-error-state
          title="Could not load your orders"
          [error]="err"
          (retry)="orders.reload()"
        />
      } @else if (!orders.hasValue()) {
        <app-loading-state label="Loading your orders" [rows]="3" />
      } @else if (orders.value().items.length === 0) {
        <app-empty-state
          title="No orders by hand yet"
          message="Orders you place on this page show here, and a working one can be changed or cancelled."
        />
      } @else {
        <app-data-table
          caption="Orders you placed by hand, newest first"
          [rows]="orders.value().items"
          [columns]="columns"
          [rowKey]="key"
          [pageSize]="20"
          [initialSort]="{ key: 'created_at', dir: 'desc' }"
        >
          <ng-template appCell="ticker" [appCellOf]="orders.value().items" let-o>
            <a class="cell-link" [routerLink]="['/trades/orders', o.client_id]">{{ o.ticker }}</a>
          </ng-template>
          <ng-template appCell="side" [appCellOf]="orders.value().items" let-o>
            <app-side-tag [side]="o.side" />
          </ng-template>
          <ng-template appCell="status" [appCellOf]="orders.value().items" let-o>
            <app-order-status [status]="o.status" [reason]="reason(o)" />
          </ng-template>
          <ng-template appCell="actions" [appCellOf]="orders.value().items" let-o>
            @if (working(o) && canTrade()) {
              <span class="row-actions">
                <button
                  type="button"
                  class="btn btn-ghost"
                  [disabled]="busy() === o.client_id"
                  (click)="change(o)"
                >
                  Change<span class="visually-hidden"> the order for {{ o.ticker }}</span>
                </button>
                <button
                  type="button"
                  class="btn btn-ghost"
                  [disabled]="busy() === o.client_id"
                  (click)="cancel(o)"
                >
                  Cancel<span class="visually-hidden"> the order for {{ o.ticker }}</span>
                </button>
              </span>
            } @else {
              <span class="muted">None</span>
            }
          </ng-template>
        </app-data-table>
      }
    </section>
    <app-status-change-dialog />
    <app-order-change-sheet />
  `,
  styles: `
    :host {
      display: block;
      min-width: 0;
    }
    .row-actions {
      display: inline-flex;
      flex-wrap: wrap;
      gap: var(--space-1);
    }
  `,
})
export class ManualOrdersList {
  private readonly ordersApi = inject(OrdersService);
  private readonly manual = inject(ManualOrdersService);
  private readonly ctx = inject(PortfolioContextService);
  private readonly session = inject(SessionService);
  private readonly stepUp = inject(StepUpService);
  private readonly toasts = inject(ToastService);
  private readonly dialog = viewChild.required(StatusChangeDialog);
  private readonly sheet = viewChild.required(OrderChangeSheet);

  protected readonly orders = resource({
    params: () => ({ portfolio: this.ctx.selectedId() }),
    loader: () => this.ordersApi.list({ origin: 'manual', limit: 50 }),
  });
  protected readonly busy = signal<string | null>(null);
  protected readonly canTrade = computed(() => this.session.can('portfolio.trade'));

  protected readonly columns: TableColumn<OrderView>[] = [
    { key: 'ticker', label: 'Ticker', mobile: 'title' },
    { key: 'side', label: 'Side' },
    { key: 'quantity', label: 'Qty', format: 'number' },
    { key: 'limit_price', label: 'Limit', format: 'money', mobile: 'hide' },
    { key: 'status', label: 'Status' },
    { key: 'created_at', label: 'Placed', format: 'datetime' },
    { key: 'actions', label: 'Actions', sortable: false, value: () => null },
  ];
  protected readonly key = (o: OrderView) => o.client_id;
  protected readonly reason = orderReason;
  protected readonly working = isWorking;

  reload(): void {
    this.orders.reload();
  }

  protected async change(order: OrderView): Promise<void> {
    const live = this.ctx.live();
    if (live && !(await this.stepUp.ensure(`Change your real-money order for ${order.ticker}.`))) {
      return;
    }
    const result = await this.sheet().open(order, live);
    if (!result) return;
    this.toasts.success(
      `Changed the order for ${order.ticker}: ${formatNumber(result.quantity)} now.`,
    );
    this.orders.reload();
  }

  protected async cancel(order: OrderView): Promise<void> {
    const body = await this.dialog().open({
      title: `Cancel the order for ${formatNumber(order.quantity)} ${order.ticker}?`,
      message: 'What has filled stays filled. The rest of the order stops.',
      confirmLabel: 'Cancel order',
      tone: 'danger',
      minReason: 1,
      reasonHint: 'Kept with the order.',
    });
    if (!body) return;
    this.busy.set(order.client_id);
    try {
      const result = await this.manual.cancel(order.client_id, { reason: body.reason ?? '' });
      this.toasts.success(
        result.cancelled
          ? `Cancelled the order for ${order.ticker}.`
          : `The order for ${order.ticker} had already ended.`,
      );
      this.orders.reload();
    } catch {
      // The error interceptor already showed the API's message.
    } finally {
      this.busy.set(null);
    }
  }
}
