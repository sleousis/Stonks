import {
  ChangeDetectionStrategy,
  Component,
  type OnInit,
  computed,
  inject,
  input,
  signal,
  viewChild,
} from '@angular/core';
import { RouterLink } from '@angular/router';

import { ManualOrdersService } from '../../api/manual-orders.service';
import type { ManualOrderRequest, ManualOrderResult } from '../../api/models';
import { SessionService } from '../../core/auth/session.service';
import { StepUpService } from '../../core/auth/step-up.service';
import { ConfirmService, type TicketLine } from '../../core/confirm/confirm.service';
import { formatMoney, formatNumber } from '../../core/format/format';
import { ApiError, errorMessage } from '../../core/http/api-error';
import { ToastService } from '../../core/notify/toast.service';
import { PortfolioContextService } from '../../core/portfolio/portfolio-context.service';
import { ModeStamp } from '../../shared/ui/mode-stamp';
import { PermissionNote } from '../../shared/ui/permission-note';
import { type SegmentOption, Segmented } from '../../shared/ui/segmented';
import { SideTag } from '../../shared/ui/side-tag';
import { EarningsWarningLine } from './earnings-warning';
import { ManualOrdersList } from './manual-orders-list';
import { OrderRefusalPanel, type OrderRefusal, refusalOf } from './order-refusal';
import { OrderStatus } from './order-status';

type Side = 'buy' | 'sell';
type OrderType = 'market' | 'limit';
type TicketErrors = Partial<Record<'ticker' | 'quantity' | 'limit' | 'reason', string>>;

const SIDES: SegmentOption<Side>[] = [
  { value: 'buy', label: 'Buy' },
  { value: 'sell', label: 'Sell' },
];
const TYPES: SegmentOption<OrderType>[] = [
  { value: 'market', label: 'Market' },
  { value: 'limit', label: 'Limit' },
];

/** A fresh idempotency key: the same key places an order once. */
function newKey(): string {
  const rand =
    typeof crypto !== 'undefined' && 'randomUUID' in crypto
      ? crypto.randomUUID().replace(/-/g, '').slice(0, 16)
      : Math.random().toString(36).slice(2, 18);
  return `console-${rand}`;
}

/** Ticket lines for a checked order: what would be placed, at what price. */
export function orderLines(
  r: ManualOrderResult,
  portfolio: string,
  currency: string,
): TicketLine[] {
  const lines: TicketLine[] = [
    { label: 'Portfolio', value: portfolio },
    { label: 'Ticker', value: r.ticker },
    { label: 'Quantity', value: formatNumber(r.quantity) },
  ];
  if (r.quantity !== r.requested_quantity) {
    lines.push({ label: 'Asked for', value: formatNumber(r.requested_quantity) });
  }
  lines.push({
    label: 'Type',
    value:
      r.order_type === 'limit'
        ? `Limit at ${formatMoney(r.limit_price, { currency })}`
        : 'Market, at the next price',
  });
  lines.push({ label: 'Last close', value: formatMoney(r.reference_price, { currency }) });
  lines.push({
    label: 'About',
    value: formatMoney(r.quantity * (r.limit_price ?? r.reference_price), { currency }),
  });
  return lines;
}

/**
 * Place an order by hand on the picked portfolio. The trader fills the
 * ticket, checks it (every halt and risk rule runs, nothing is placed), then
 * places it. A real-money book asks for a fresh code first and the ticker
 * typed on the ticket. A refused order shows each rule's adjustment, and
 * when the rules allow a smaller order the trader may accept it.
 *
 * `?ticker=&side=` prefill the ticket (from a chart or a holding).
 */
@Component({
  selector: 'app-manual-ticket-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    RouterLink,
    Segmented,
    SideTag,
    ModeStamp,
    PermissionNote,
    OrderRefusalPanel,
    OrderStatus,
    ManualOrdersList,
    EarningsWarningLine,
  ],
  template: `
    <div class="layout">
      <section class="panel" aria-labelledby="ticket-form-title">
        <div class="panel-head">
          <h2 id="ticket-form-title">New order</h2>
          <app-mode-stamp [live]="live()" />
        </div>
        <form class="panel-body form" novalidate (submit)="$event.preventDefault(); place()">
          <p class="lead">
            Trades in <strong>{{ portfolioName() }}</strong
            >. It goes through the same halts and risk limits as a strategy's order.
            @if (live()) {
              This portfolio trades real money.
            }
          </p>

          <div class="field">
            <label for="mo-ticker">Ticker</label>
            <input
              id="mo-ticker"
              class="input"
              autocomplete="off"
              autocapitalize="characters"
              placeholder="AAPL.US"
              [value]="tickerField()"
              [attr.aria-invalid]="!!errors().ticker"
              [attr.aria-describedby]="errors().ticker ? 'mo-ticker-error' : null"
              (input)="edit(tickerField, $any($event.target).value.toUpperCase())"
            />
            @if (errors().ticker; as e) {
              <span id="mo-ticker-error" class="hint error">{{ e }}</span>
            }
          </div>
          <app-earnings-warning [ticker]="tickerField()" />

          <div class="field">
            <span class="label">Side</span>
            <app-segmented
              label="Side"
              emphasis="strong"
              [options]="sides"
              [value]="sideField()"
              (valueChange)="edit(sideField, $any($event))"
            />
            <span class="side-now" aria-live="polite">
              <app-side-tag [side]="sideField()" />
              {{ sideField() === 'buy' ? 'You are buying' : 'You are selling' }}
            </span>
          </div>

          <div class="field">
            <label for="mo-qty">Quantity</label>
            <input
              id="mo-qty"
              class="input num"
              type="number"
              inputmode="decimal"
              min="0"
              step="any"
              [value]="quantity()"
              [attr.aria-invalid]="!!errors().quantity"
              [attr.aria-describedby]="errors().quantity ? 'mo-qty-error' : null"
              (input)="edit(quantity, $any($event.target).value)"
            />
            @if (errors().quantity; as e) {
              <span id="mo-qty-error" class="hint error">{{ e }}</span>
            }
          </div>

          <div class="field">
            <span class="label">Order type</span>
            <app-segmented
              label="Order type"
              emphasis="strong"
              [options]="types"
              [value]="orderType()"
              (valueChange)="edit(orderType, $any($event))"
            />
          </div>

          @if (orderType() === 'limit') {
            <div class="field">
              <label for="mo-limit">Limit price</label>
              <input
                id="mo-limit"
                class="input num"
                type="number"
                inputmode="decimal"
                min="0"
                step="any"
                [value]="limit()"
                [attr.aria-invalid]="!!errors().limit"
                [attr.aria-describedby]="errors().limit ? 'mo-limit-error' : 'mo-limit-hint'"
                (input)="edit(limit, $any($event.target).value)"
              />
              @if (errors().limit; as e) {
                <span id="mo-limit-error" class="hint error">{{ e }}</span>
              } @else {
                <span id="mo-limit-hint" class="hint">
                  A paper portfolio fills at the last close, and only when that close is at or
                  better than this. Otherwise the order is refused.
                </span>
              }
            </div>
          }

          <div class="field">
            <label for="mo-reason">Why this trade</label>
            <textarea
              id="mo-reason"
              class="input"
              rows="2"
              maxlength="500"
              [value]="reason()"
              [attr.aria-invalid]="!!errors().reason"
              [attr.aria-describedby]="errors().reason ? 'mo-reason-error' : 'mo-reason-hint'"
              (input)="reason.set($any($event.target).value)"
            ></textarea>
            @if (errors().reason; as e) {
              <span id="mo-reason-error" class="hint error">{{ e }}</span>
            } @else {
              <span id="mo-reason-hint" class="hint">Kept with the order and in your journal.</span>
            }
          </div>

          <div class="actions">
            <button type="button" class="btn" [disabled]="busy()" (click)="check()">
              Check order
            </button>
            <button type="submit" class="btn btn-primary" [disabled]="busy() || !canTrade()">
              Place order
            </button>
            <app-permission-note permission="portfolio.trade" />
          </div>
        </form>
      </section>

      <aside class="side" aria-label="Order ticket">
        @if (preview(); as p) {
          <div class="ticket" [class.live]="live()" role="group" aria-label="Checked order">
            <p class="ticket-head">
              <span class="ticket-kind">Order ticket</span>
              <app-side-tag [side]="p.side" />
              <app-mode-stamp [live]="live()" />
            </p>
            <dl class="ticket-lines">
              @for (line of previewLines(); track line.label) {
                <div>
                  <dt>{{ line.label }}</dt>
                  <dd class="num">{{ line.value }}</dd>
                </div>
              }
            </dl>
            @if (p.halt) {
              <p class="note warn">{{ p.halt }}</p>
            }
            @if (p.quantity < p.requested_quantity) {
              <p class="note">
                The risk limits cut this order to {{ num(p.quantity) }}. Placing it places the
                smaller order.
              </p>
            } @else {
              <p class="note ok">Every check passed. Nothing is placed until you place it.</p>
            }
          </div>
        } @else if (!refusal() && !failure()) {
          <div class="ticket blank" role="group" aria-label="Order ticket">
            <p class="ticket-head">
              <span class="ticket-kind">Order ticket</span>
              <app-mode-stamp [live]="live()" />
            </p>
            <p class="muted">Fill in the order and check it. The price and the checks show here.</p>
          </div>
        }

        @if (refusal(); as r) {
          <app-order-refusal
            [refusal]="r"
            [allowReduce]="allowReduce()"
            (allowReduceChange)="setAllowReduce($event)"
          />
        }
        @if (failure(); as f) {
          <p class="failure" role="alert">{{ f }}</p>
        }

        @if (placed(); as r) {
          <div class="result" role="status">
            <p class="result-head">
              <app-side-tag [side]="r.side" />
              <span class="num">{{ num(r.quantity) }} {{ r.ticker }}</span>
              <app-order-status [status]="r.status" [reason]="r.reason ?? null" />
            </p>
            @if (r.fill_price !== null && r.fill_price !== undefined) {
              <p class="num">Filled at {{ money(r.fill_price) }}</p>
            }
            <a class="btn btn-ghost" [routerLink]="['/trades/orders', r.client_id]"
              >Open the order</a
            >
          </div>
        }
      </aside>
    </div>

    <app-manual-orders-list />
  `,
  styles: `
    @use 'breakpoints' as bp;

    :host {
      display: grid;
      gap: var(--space-5);
      min-width: 0;
    }
    .layout {
      display: grid;
      gap: var(--space-4);
      min-width: 0;
      @include bp.from-desktop {
        grid-template-columns: minmax(0, 3fr) minmax(0, 2fr);
        align-items: start;
      }
    }
    .form {
      display: grid;
      gap: var(--space-4);
    }
    .lead {
      color: var(--color-ink-2);
      font-size: var(--text-sm);
    }
    .label {
      font-size: var(--text-sm);
      font-weight: var(--weight-medium);
    }
    .side-now {
      display: inline-flex;
      align-items: center;
      gap: var(--space-2);
      color: var(--color-ink-2);
      font-size: var(--text-sm);
    }
    .actions {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: var(--space-2);
    }
    .side {
      display: grid;
      gap: var(--space-3);
      min-width: 0;
    }
    .ticket.blank {
      box-shadow: none;
    }
    .note {
      font-size: var(--text-sm);
      color: var(--color-ink-2);
    }
    .note.ok {
      color: var(--color-gain);
    }
    .note.warn {
      color: var(--color-warn);
    }
    .failure {
      padding: var(--space-3) var(--space-4);
      border-left: 3px solid var(--color-loss);
      border-radius: var(--radius-sm);
      background: var(--color-loss-soft);
      overflow-wrap: anywhere;
    }
    .result {
      display: grid;
      justify-items: start;
      gap: var(--space-2);
      padding: var(--space-3) var(--space-4);
      border: 1px solid var(--color-border);
      border-radius: var(--radius-md);
      background: var(--color-surface);
    }
    .result-head {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: var(--space-2);
    }
  `,
})
export class ManualTicketPage implements OnInit {
  private readonly api = inject(ManualOrdersService);
  private readonly ctx = inject(PortfolioContextService);
  private readonly session = inject(SessionService);
  private readonly stepUp = inject(StepUpService);
  private readonly confirm = inject(ConfirmService);
  private readonly toasts = inject(ToastService);

  /** Query params that prefill the ticket. */
  readonly ticker = input<string>();
  readonly side = input<string>();

  protected readonly sides = SIDES;
  protected readonly types = TYPES;

  protected readonly tickerField = signal('');
  protected readonly sideField = signal<Side>('buy');
  protected readonly quantity = signal('');
  protected readonly orderType = signal<OrderType>('market');
  protected readonly limit = signal('');
  protected readonly reason = signal('');
  protected readonly allowReduce = signal(false);

  protected readonly busy = signal(false);
  protected readonly tried = signal(false);
  protected readonly preview = signal<ManualOrderResult | null>(null);
  protected readonly refusal = signal<OrderRefusal | null>(null);
  protected readonly failure = signal<string | null>(null);
  protected readonly placed = signal<ManualOrderResult | null>(null);
  private key = newKey();

  private readonly list = viewChild(ManualOrdersList);

  protected readonly live = computed(() => this.ctx.live());
  protected readonly portfolioName = computed(() => this.ctx.current()?.name ?? 'your portfolio');
  private readonly currency = computed(() => this.ctx.current()?.base_currency ?? 'USD');
  protected readonly canTrade = computed(() => this.session.can('portfolio.trade'));

  protected readonly errors = computed(() => {
    const out: TicketErrors = {};
    if (!this.tried()) return out;
    if (!this.tickerField().trim()) out.ticker = 'Enter a ticker, like AAPL.US.';
    const qty = Number(this.quantity());
    if (!(qty > 0)) out.quantity = 'Enter a quantity above zero.';
    if (this.orderType() === 'limit' && !(Number(this.limit()) > 0)) {
      out.limit = 'Enter a limit price above zero.';
    }
    if (!this.reason().trim()) out.reason = 'Say why, in a few words.';
    return out;
  });

  protected readonly previewLines = computed(() => {
    const p = this.preview();
    return p ? orderLines(p, this.portfolioName(), this.currency()) : [];
  });

  ngOnInit(): void {
    const t = this.ticker();
    if (t) this.tickerField.set(t.toUpperCase());
    const s = this.side();
    if (s === 'buy' || s === 'sell') this.sideField.set(s);
  }

  protected readonly num = (v: number) => formatNumber(v);
  protected money(v: number): string {
    return formatMoney(v, { currency: this.currency() });
  }

  /** Any change to the order makes the old check stale and needs a new key. */
  protected edit<T>(field: { set(v: T): void }, value: T): void {
    field.set(value);
    this.key = newKey();
    this.preview.set(null);
    this.refusal.set(null);
    this.failure.set(null);
    this.allowReduce.set(false);
  }

  protected async setAllowReduce(on: boolean): Promise<void> {
    this.allowReduce.set(on);
    if (on) await this.check();
  }

  /** Run every check without placing anything. */
  async check(): Promise<ManualOrderResult | null> {
    const body = this.request();
    if (!body) return null;
    this.busy.set(true);
    this.failure.set(null);
    this.placed.set(null);
    try {
      const result = await this.api.preview(body);
      this.preview.set(result);
      this.refusal.set(null);
      return result;
    } catch (err) {
      this.showFailure(err);
      return null;
    } finally {
      this.busy.set(false);
    }
  }

  /** Check, confirm on the ticket (a fresh code first for real money), then place. */
  async place(): Promise<void> {
    if (!this.request()) return;
    const live = this.live();
    if (live) {
      const ok = await this.stepUp.ensure(`Place a real-money order for ${this.tickerField()}.`);
      if (!ok) return;
    }
    const checked = await this.check();
    if (!checked) return;
    const ok = await this.confirm.confirm({
      title: `${checked.side === 'buy' ? 'Buy' : 'Sell'} ${formatNumber(checked.quantity)} ${checked.ticker}?`,
      message: live
        ? 'This order goes to your broker with real money.'
        : 'This order trades with pretend money in your paper portfolio.',
      confirmLabel: 'Place order',
      tone: live ? 'danger' : 'default',
      typedConfirmation: live ? checked.ticker : undefined,
      ticket: {
        side: checked.side,
        live,
        lines: orderLines(checked, this.portfolioName(), this.currency()),
      },
    });
    if (!ok) return;
    const body = this.request();
    if (!body) return;
    this.busy.set(true);
    try {
      const result = await this.api.place(body);
      this.placed.set(result);
      this.preview.set(null);
      this.toasts.success(this.placedText(result));
      this.quantity.set('');
      this.reason.set('');
      this.limit.set('');
      this.allowReduce.set(false);
      this.tried.set(false);
      this.key = newKey();
      this.list()?.reload();
    } catch (err) {
      this.showFailure(err);
    } finally {
      this.busy.set(false);
    }
  }

  private placedText(r: ManualOrderResult): string {
    const what = `${r.side === 'buy' ? 'Bought' : 'Sold'} ${formatNumber(r.quantity)} ${r.ticker}`;
    if (r.duplicate) return `This order was already placed. ${r.ticker} is unchanged.`;
    if (r.status === 'filled')
      return `${what} at ${this.money(r.fill_price ?? r.reference_price)}.`;
    if (r.status === 'rejected')
      return `Order for ${r.ticker} was rejected: ${r.reason ?? 'no reason given'}.`;
    return `Placed the order for ${formatNumber(r.quantity)} ${r.ticker}. It is working.`;
  }

  private showFailure(err: unknown): void {
    this.preview.set(null);
    const refused = refusalOf(err);
    if (refused) {
      this.refusal.set(refused);
      return;
    }
    this.refusal.set(null);
    if (err instanceof ApiError && err.code === 'step_up_required') {
      this.failure.set('Not placed. A real-money order needs a fresh code from your app.');
      return;
    }
    this.failure.set(errorMessage(err));
  }

  private request(): ManualOrderRequest | null {
    this.tried.set(true);
    if (Object.keys(this.errors()).length) return null;
    const limit = this.orderType() === 'limit' ? Number(this.limit()) : null;
    return {
      ticker: this.tickerField().trim().toUpperCase(),
      side: this.sideField(),
      quantity: Number(this.quantity()),
      order_type: this.orderType(),
      limit_price: limit,
      reason: this.reason().trim(),
      allow_reduce: this.allowReduce(),
      client_id: this.key,
    };
  }
}
