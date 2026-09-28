import { ChangeDetectionStrategy, Component, computed, inject, signal } from '@angular/core';

import { ManualOrdersService } from '../../api/manual-orders.service';
import type { ManualOrderResult, OrderView } from '../../api/models';
import { formatNumber } from '../../core/format/format';
import { ApiError, errorMessage } from '../../core/http/api-error';
import { ModeStamp } from '../../shared/ui/mode-stamp';
import { Sheet } from '../../shared/ui/sheet';
import { SideTag } from '../../shared/ui/side-tag';
import { OrderRefusalPanel, type OrderRefusal, refusalOf } from './order-refusal';

interface Open {
  order: OrderView;
  live: boolean;
  resolve: (result: ManualOrderResult | null) => void;
}

/**
 * Change a working manual order: a new quantity or limit and a reason. The
 * server cancels the working order and places the new one through every
 * check, so a refusal shows here with each rule's adjustment and the choice
 * to accept a smaller order. Resolves to the new order, or null.
 *
 *   const result = await this.sheet().open(order, live);
 */
@Component({
  selector: 'app-order-change-sheet',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [Sheet, SideTag, ModeStamp, OrderRefusalPanel],
  template: `
    <app-sheet
      [open]="!!current()"
      labelledBy="change-order-title"
      describedBy="change-order-message"
      (dismiss)="close(null)"
    >
      @if (current(); as c) {
        <form class="sheet-form" novalidate (submit)="$event.preventDefault(); submit()">
          <h2 id="change-order-title">Change the order for {{ c.order.ticker }}</h2>
          <p class="line">
            <app-side-tag [side]="c.order.side" />
            <span class="num">{{ num(c.order.quantity) }} {{ c.order.ticker }}</span>
            <app-mode-stamp [live]="c.live" />
          </p>
          <p id="change-order-message" class="sheet-message">
            The working order is cancelled and a new one is placed through every check.
          </p>
          <div class="field">
            <label for="co-qty">New quantity</label>
            <input
              id="co-qty"
              class="input num"
              type="number"
              inputmode="decimal"
              min="0"
              step="any"
              [value]="quantity()"
              (input)="quantity.set($any($event.target).value); refusal.set(null)"
            />
          </div>
          @if (c.order.order_type === 'limit') {
            <div class="field">
              <label for="co-limit">New limit price</label>
              <input
                id="co-limit"
                class="input num"
                type="number"
                inputmode="decimal"
                min="0"
                step="any"
                [value]="limit()"
                (input)="limit.set($any($event.target).value); refusal.set(null)"
              />
            </div>
          }
          <div class="field">
            <label for="co-reason">Why change it</label>
            <textarea
              id="co-reason"
              class="input"
              rows="2"
              maxlength="500"
              aria-describedby="co-reason-hint"
              [value]="reason()"
              (input)="reason.set($any($event.target).value)"
            ></textarea>
            <span id="co-reason-hint" class="hint" [class.error]="tried() && !valid()">
              {{ tried() && !valid() ? problem() : 'Kept with the order.' }}
            </span>
          </div>
          @if (refusal(); as r) {
            <app-order-refusal [refusal]="r" [(allowReduce)]="allowReduce" />
          }
          @if (failure(); as f) {
            <p class="failure" role="alert">{{ f }}</p>
          }
          <div class="sheet-actions">
            <button type="button" class="btn" (click)="close(null)">Keep the order</button>
            <button
              type="submit"
              class="btn"
              [class.btn-danger]="c.live"
              [class.btn-primary]="!c.live"
              [disabled]="busy()"
            >
              Change order
            </button>
          </div>
        </form>
      }
    </app-sheet>
  `,
  styles: `
    .line {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: var(--space-2);
    }
    .failure {
      padding: var(--space-3);
      border-left: 3px solid var(--color-loss);
      background: var(--color-loss-soft);
      overflow-wrap: anywhere;
    }
  `,
})
export class OrderChangeSheet {
  private readonly api = inject(ManualOrdersService);

  protected readonly current = signal<Open | null>(null);
  protected readonly quantity = signal('');
  protected readonly limit = signal('');
  protected readonly reason = signal('');
  protected readonly allowReduce = signal(false);
  protected readonly refusal = signal<OrderRefusal | null>(null);
  protected readonly failure = signal<string | null>(null);
  protected readonly busy = signal(false);
  protected readonly tried = signal(false);

  protected readonly problem = computed(() => {
    const c = this.current();
    if (!(Number(this.quantity()) > 0)) return 'Enter a quantity above zero.';
    if (c?.order.order_type === 'limit' && !(Number(this.limit()) > 0)) {
      return 'Enter a limit price above zero.';
    }
    if (!this.reason().trim()) return 'Say why, in a few words.';
    return null;
  });
  protected readonly valid = computed(() => this.problem() === null);
  protected readonly num = (v: number) => formatNumber(v);

  open(order: OrderView, live: boolean): Promise<ManualOrderResult | null> {
    this.current()?.resolve(null);
    this.quantity.set(String(order.quantity));
    this.limit.set(order.limit_price === null ? '' : String(order.limit_price));
    this.reason.set('');
    this.allowReduce.set(false);
    this.refusal.set(null);
    this.failure.set(null);
    this.tried.set(false);
    return new Promise((resolve) => this.current.set({ order, live, resolve }));
  }

  protected close(result: ManualOrderResult | null): void {
    const c = this.current();
    this.current.set(null);
    c?.resolve(result);
  }

  protected async submit(): Promise<void> {
    const c = this.current();
    if (!c) return;
    this.tried.set(true);
    if (!this.valid()) return;
    this.busy.set(true);
    this.failure.set(null);
    try {
      const result = await this.api.change(c.order.client_id, {
        quantity: Number(this.quantity()),
        limit_price: c.order.order_type === 'limit' ? Number(this.limit()) : null,
        reason: this.reason().trim(),
        allow_reduce: this.allowReduce(),
      });
      this.close(result);
    } catch (err) {
      const refused = refusalOf(err);
      this.refusal.set(refused);
      if (!refused) {
        this.failure.set(
          err instanceof ApiError && err.code === 'step_up_required'
            ? 'Not changed. A real-money order needs a fresh code from your app.'
            : errorMessage(err),
        );
      }
    } finally {
      this.busy.set(false);
    }
  }
}
