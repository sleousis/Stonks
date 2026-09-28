import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  input,
  linkedSignal,
  resource,
  signal,
} from '@angular/core';

import type { FillView, LotPickView } from '../../api/models';
import { OrdersService } from '../../api/orders.service';
import { TaxService } from '../../api/tax.service';
import { formatDateTime, formatMoney, formatNumber } from '../../core/format/format';
import { ToastService } from '../../core/notify/toast.service';
import { PermissionNote } from '../../shared/ui/permission-note';
import { EmptyState, ErrorState, LoadingState } from '../../shared/ui/states';

const FILLS_PAGE = 500;

/** The buy lots a sale could close: same ticker, bought before the sale. */
export function candidateLots(fills: readonly FillView[], sale: FillView): FillView[] {
  const at = Date.parse(sale.filled_at);
  return fills
    .filter(
      (f) =>
        f.side === 'buy' &&
        f.ticker === sale.ticker &&
        f.id !== sale.id &&
        Date.parse(f.filled_at) <= at,
    )
    .sort((a, b) => Date.parse(a.filled_at) - Date.parse(b.filled_at));
}

/** Why the picks cannot be saved, or null. */
export function picksError(
  sale: FillView,
  lots: readonly FillView[],
  picked: Readonly<Record<number, string>>,
): string | null {
  let total = 0;
  for (const lot of lots) {
    const raw = (picked[lot.id] ?? '').trim();
    if (!raw) continue;
    const qty = Number(raw);
    if (!Number.isFinite(qty) || qty < 0) return 'Each amount must be 0 or more.';
    if (qty > lot.quantity) return `A lot has only ${formatNumber(lot.quantity)} shares.`;
    total += qty;
  }
  if (total > sale.quantity + 1e-9) {
    return `The sale sold ${formatNumber(sale.quantity)} shares, and the picks add up to ${formatNumber(total)}.`;
  }
  return null;
}

/**
 * Specific lots: pick which buys a sale closes. The server closes what you
 * leave out oldest first (FIFO). Shown on the tax page when the lot method
 * is "specific".
 */
@Component({
  selector: 'app-lot-picks',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [PermissionNote, LoadingState, EmptyState, ErrorState],
  template: `
    @if (fills.error(); as err) {
      <app-error-state title="Could not load your fills" [error]="err" (retry)="fills.reload()" />
    } @else if (!fills.hasValue()) {
      <app-loading-state label="Loading your sales" [rows]="3" />
    } @else if (sales().length === 0) {
      <app-empty-state
        title="No sales yet"
        message="Once this portfolio sells, pick here which buys each sale closes."
      />
    } @else {
      <div class="field">
        <label for="lot-sale">Sale</label>
        <select
          id="lot-sale"
          class="input"
          [value]="saleId() ?? ''"
          (change)="pickSale($any($event.target).value)"
        >
          <option value="">Choose a sale</option>
          @for (s of sales(); track s.id) {
            <option [value]="s.id">{{ saleLabel(s) }}</option>
          }
        </select>
      </div>

      @if (sale(); as s) {
        @if (picks.error(); as err) {
          <app-error-state
            title="Could not load the picks"
            [error]="err"
            (retry)="picks.reload()"
          />
        } @else if (!picks.hasValue()) {
          <app-loading-state label="Loading the picks" [rows]="2" />
        } @else if (lots().length === 0) {
          <p class="muted">
            No buy of {{ s.ticker }} before this sale, so there is nothing to pick.
          </p>
        } @else {
          <form class="lots" (submit)="$event.preventDefault(); save()" novalidate>
            <fieldset>
              <legend>Shares to close from each buy</legend>
              <ul>
                @for (lot of lots(); track lot.id) {
                  <li>
                    <label [for]="'lot-' + lot.id">
                      <span class="num">{{ when(lot.filled_at) }}</span>
                      <span class="muted num"
                        >{{ num(lot.quantity) }} at {{ money(lot.price) }}</span
                      >
                    </label>
                    <input
                      class="input num"
                      type="number"
                      inputmode="decimal"
                      min="0"
                      [max]="lot.quantity"
                      [id]="'lot-' + lot.id"
                      [disabled]="!canManage()"
                      [value]="picked()[lot.id] ?? ''"
                      (input)="setQty(lot.id, $any($event.target).value)"
                    />
                  </li>
                }
              </ul>
            </fieldset>
            <p class="total" [class.error]="!!error()" role="status">
              {{ error() ?? totalText() }}
            </p>
            <div class="actions">
              <button
                type="button"
                class="btn"
                [disabled]="!canManage() || busy()"
                (click)="clear()"
              >
                Use oldest first
              </button>
              <button
                type="submit"
                class="btn btn-primary"
                [disabled]="!canManage() || busy() || !!error()"
                [attr.aria-busy]="busy()"
              >
                Save picks
              </button>
              <app-permission-note permission="portfolio.manage" />
            </div>
          </form>
        }
      }
    }
  `,
  styles: `
    @use 'breakpoints' as bp;
    :host {
      display: grid;
      gap: var(--space-3);
    }
    fieldset {
      margin: 0;
      padding: 0;
      border: 0;
    }
    legend {
      margin-bottom: var(--space-2);
      font-size: var(--text-sm);
      font-weight: var(--weight-medium);
    }
    ul {
      display: grid;
      margin: 0;
      padding: 0;
      list-style: none;
    }
    li {
      display: grid;
      grid-template-columns: minmax(0, 1fr) 8rem;
      align-items: center;
      gap: var(--space-2) var(--space-3);
      padding: var(--space-2) 0;
      border-top: 1px solid var(--color-border);
      @include bp.phone {
        grid-template-columns: minmax(0, 1fr) 6.5rem;
      }
    }
    label {
      display: grid;
      gap: 2px;
      min-width: 0;
      font-size: var(--text-sm);
    }
    .total {
      margin: var(--space-2) 0;
      font-size: var(--text-sm);
      color: var(--color-ink-2);
    }
    .total.error {
      color: var(--color-loss);
    }
    .actions {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: var(--space-2);
    }
  `,
})
export class LotPicks {
  private readonly orders = inject(OrdersService);
  private readonly tax = inject(TaxService);
  private readonly toasts = inject(ToastService);

  /** The portfolio shown (reloads the lists when it changes). */
  readonly portfolioId = input<string | null>(null);
  readonly canManage = input(false);

  protected readonly fills = resource({
    params: () => ({ portfolio: this.portfolioId() }),
    loader: () => this.orders.fills({ limit: FILLS_PAGE }),
  });
  protected readonly sales = computed(() =>
    this.fills.hasValue()
      ? this.fills
          .value()
          .items.filter((f) => f.side === 'sell')
          .sort((a, b) => Date.parse(b.filled_at) - Date.parse(a.filled_at))
      : [],
  );

  protected readonly saleId = linkedSignal<string | null, number | null>({
    source: () => this.portfolioId(),
    computation: () => null,
  });
  protected readonly sale = computed(
    () => this.sales().find((s) => s.id === this.saleId()) ?? null,
  );
  protected readonly lots = computed(() => {
    const s = this.sale();
    return s && this.fills.hasValue() ? candidateLots(this.fills.value().items, s) : [];
  });

  protected readonly picks = resource({
    params: () => {
      const id = this.saleId();
      return id == null ? undefined : { id };
    },
    loader: ({ params }) => this.tax.picks(params.id),
  });
  protected readonly picked = linkedSignal<LotPickView[] | undefined, Record<number, string>>({
    source: () => (this.picks.hasValue() ? this.picks.value().items : undefined),
    computation: (items) =>
      Object.fromEntries((items ?? []).map((p) => [p.buy_fill_id, String(p.quantity)])),
  });

  protected readonly error = computed(() => {
    const s = this.sale();
    return s ? picksError(s, this.lots(), this.picked()) : null;
  });
  protected readonly totalText = computed(() => {
    const s = this.sale();
    if (!s) return '';
    const total = Object.values(this.picked()).reduce((sum, v) => sum + (Number(v) || 0), 0);
    const rest = Math.max(0, s.quantity - total);
    return rest > 0
      ? `Picked ${this.num(total)} of ${this.num(s.quantity)}. The other ${this.num(rest)} close oldest first.`
      : `Picked all ${this.num(s.quantity)} shares.`;
  });

  protected readonly busy = signal(false);

  protected readonly when = (at: string) => formatDateTime(at);
  protected readonly num = (v: number) => formatNumber(v);
  protected readonly money = (v: number) => formatMoney(v);
  protected readonly saleLabel = (s: FillView) =>
    `${s.ticker}: sold ${formatNumber(s.quantity)} on ${formatDateTime(s.filled_at)}`;

  protected pickSale(value: string): void {
    this.saleId.set(value ? Number(value) : null);
  }

  protected setQty(lotId: number, value: string): void {
    this.picked.update((p) => ({ ...p, [lotId]: value }));
  }

  protected async save(): Promise<void> {
    const s = this.sale();
    if (!s || this.error() || this.busy()) return;
    const picks = this.lots()
      .map((lot) => ({ buy_fill_id: lot.id, quantity: Number(this.picked()[lot.id] || 0) }))
      .filter((p) => p.quantity > 0);
    await this.send(s, picks, `Saved the lots for the ${s.ticker} sale.`);
  }

  protected async clear(): Promise<void> {
    const s = this.sale();
    if (!s || this.busy()) return;
    await this.send(s, [], `The ${s.ticker} sale closes its oldest lots first again.`);
  }

  private async send(
    s: FillView,
    picks: { buy_fill_id: number; quantity: number }[],
    done: string,
  ): Promise<void> {
    this.busy.set(true);
    try {
      await this.tax.setPicks({ sell_fill_id: s.id, picks });
      this.toasts.success(done);
      this.picks.reload();
    } catch {
      // The error interceptor already showed the API's message.
    } finally {
      this.busy.set(false);
    }
  }
}
