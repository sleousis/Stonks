import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  input,
  output,
  signal,
  viewChild,
} from '@angular/core';
import { RouterLink } from '@angular/router';

import type { OrderDraftView } from '../../api/models';
import { OrderDraftsService } from '../../api/order-drafts.service';
import { SessionService } from '../../core/auth/session.service';
import { StepUpService } from '../../core/auth/step-up.service';
import { ConfirmService, type TicketLine } from '../../core/confirm/confirm.service';
import { formatAgo, formatDateTime, formatMoney, formatNumber } from '../../core/format/format';
import { ApiError, errorMessage } from '../../core/http/api-error';
import { ToastService } from '../../core/notify/toast.service';
import { PortfolioContextService } from '../../core/portfolio/portfolio-context.service';
import { ModeStamp } from '../../shared/ui/mode-stamp';
import { SideTag } from '../../shared/ui/side-tag';
import { StatusChangeDialog } from '../../shared/ui/status-change-dialog';
import { StatusPill, type PillTone } from '../../shared/ui/status-pill';
import { refusalOf } from '../orders/order-refusal';
import { TaxPreviewPanel, type TaxQuestion } from '../orders/tax-preview';

type DraftStatus = OrderDraftView['status'];

const STATUS: Record<DraftStatus, { label: string; tone: PillTone }> = {
  pending: { label: 'Waiting for you', tone: 'progress' },
  placed: { label: 'Placed', tone: 'positive' },
  rejected: { label: 'Rejected', tone: 'negative' },
  expired: { label: 'Expired', tone: 'neutral' },
  cancelled: { label: 'Cancelled', tone: 'neutral' },
};

const SOURCES: Record<OrderDraftView['source'], string> = {
  assistant: 'the assistant',
  console: 'you, in the console',
  mcp: 'an agent tool',
};

/** A suggested order's status in trader words, with its pill tone. */
export function draftStatus(status: DraftStatus): { label: string; tone: PillTone } {
  return STATUS[status] ?? { label: status, tone: 'neutral' };
}

/** Who suggested the order, in lower case to sit inside a sentence. */
export function draftSource(source: OrderDraftView['source']): string {
  return SOURCES[source] ?? source;
}

/**
 * Suggested orders (F9): orders the assistant or an agent tool proposed,
 * shown in the one Approvals inbox next to the strategy tickets. Nothing
 * trades until the person approves one here: approving asks for a fresh
 * code, shows the order ticket, then places it as a manual order through
 * every halt and risk rule. A refused order shows the reason on its card.
 * The page owns the list; `changed` asks it to read the list again.
 */
@Component({
  selector: 'app-suggested-orders',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink, SideTag, ModeStamp, StatusPill, StatusChangeDialog, TaxPreviewPanel],
  template: `
    <ul class="tickets">
      @for (d of drafts(); track d.id) {
        <li>
          <article class="ticket" [class.live]="isLive(d)" [attr.aria-labelledby]="'draft-' + d.id">
            <p class="ticket-head">
              <app-side-tag [side]="d.side" />
              <span class="ticket-kind" [id]="'draft-' + d.id">
                <span class="verb">{{ d.side === 'buy' ? 'Buy' : 'Sell' }}</span>
                <span class="num">{{ num(d.quantity) }}</span>
                <strong class="num">{{ d.ticker }}</strong>
              </span>
              <app-mode-stamp [live]="isLive(d)" />
            </p>
            <p class="source"><span class="source-tag">Suggested</span> by {{ sourceOf(d) }}</p>
            <dl class="ticket-lines">
              @for (line of lines(d); track line.label) {
                <div>
                  <dt>{{ line.label }}</dt>
                  <dd class="num">{{ line.value }}</dd>
                </div>
              }
            </dl>
            <p class="why"><span class="muted">Why:</span> {{ d.reason }}</p>
            @if (d.status === 'pending') {
              <app-tax-preview [question]="taxOf(d)" />
            }
            <p class="meta">
              <app-status-pill
                [status]="d.status"
                [label]="statusOf(d).label"
                [tone]="statusOf(d).tone"
              />
              <span class="muted">
                {{ ago(d.created_at) }}.
                @if (d.status === 'pending') {
                  Expires {{ when(d.expires_at) }}.
                } @else if (d.decision_note) {
                  {{ d.decision_note }}
                }
              </span>
            </p>
            @if (problems()[d.id]; as p) {
              <p class="failure" role="alert">{{ p }}</p>
            }
            @if (d.client_id) {
              <a class="btn btn-ghost" [routerLink]="['/trades/orders', d.client_id]"
                >Open the order</a
              >
            }
            @if (d.status === 'pending') {
              <div class="actions">
                <button
                  type="button"
                  class="btn"
                  [disabled]="busy() === d.id || !canReject()"
                  (click)="reject(d)"
                >
                  Reject<span class="visually-hidden"> the suggested order for {{ d.ticker }}</span>
                </button>
                <button
                  type="button"
                  class="btn btn-primary"
                  [disabled]="busy() === d.id || !canApprove()"
                  (click)="approve(d)"
                >
                  Approve<span class="visually-hidden">
                    the suggested order for {{ d.ticker }}</span
                  >
                </button>
              </div>
            }
          </article>
        </li>
      }
    </ul>
    <app-status-change-dialog />
  `,
  styles: `
    @use 'breakpoints' as bp;
    :host {
      display: block;
      min-width: 0;
    }
    .tickets {
      display: grid;
      grid-template-columns: minmax(0, 1fr);
      gap: var(--space-3);
      margin: 0;
      padding: 0;
      list-style: none;

      @include bp.from-tablet {
        grid-template-columns: repeat(auto-fill, minmax(18rem, 1fr));
      }
    }
    .ticket-kind {
      display: inline-flex;
      flex-wrap: wrap;
      align-items: baseline;
      gap: var(--space-1) var(--space-2);
      color: var(--color-ink);
      font-size: var(--text-md);
      min-width: 0;
      overflow-wrap: anywhere;
    }
    .source {
      color: var(--color-ink-2);
      font-size: var(--text-sm);
    }
    .source-tag {
      display: inline-block;
      margin-right: var(--space-1);
      padding: 0 var(--space-2);
      border: 1px solid var(--color-border-strong);
      border-radius: var(--radius-xs);
      color: var(--color-ink);
      font-size: var(--text-xs);
      font-weight: var(--weight-medium);
    }
    .why {
      overflow-wrap: anywhere;
    }
    .meta {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: var(--space-2);
      font-size: var(--text-sm);
    }
    .actions {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: var(--space-2);
      padding-top: var(--space-2);
      border-top: 1px dashed var(--color-border-strong);
    }
    .actions .btn {
      min-height: var(--touch-min);
      justify-content: center;
    }
    .failure {
      padding: var(--space-2) var(--space-3);
      border-left: 3px solid var(--color-loss);
      background: var(--color-loss-soft);
      overflow-wrap: anywhere;
    }
  `,
})
export class SuggestedOrders {
  private readonly api = inject(OrderDraftsService);
  private readonly ctx = inject(PortfolioContextService);
  private readonly session = inject(SessionService);
  private readonly stepUp = inject(StepUpService);
  private readonly confirm = inject(ConfirmService);
  private readonly toasts = inject(ToastService);
  private readonly dialog = viewChild.required(StatusChangeDialog);

  /** The suggested orders to show, newest first. */
  readonly drafts = input.required<readonly OrderDraftView[]>();
  /** An order was approved or rejected: read the list again. */
  readonly changed = output<void>();

  protected readonly busy = signal<string | null>(null);
  /** The last failure per suggested order, shown on its card. */
  protected readonly problems = signal<Record<string, string>>({});
  protected readonly canApprove = computed(() => this.session.can('orders.approve'));
  protected readonly canReject = computed(() => this.session.can('portfolio.trade'));

  protected readonly num = (v: number) => formatNumber(v);

  /** The tax preview of a draft, asked before you approve it (roadmap 23.5). */
  protected taxOf(d: OrderDraftView): TaxQuestion {
    return {
      ticker: d.ticker,
      side: d.side,
      quantity: d.quantity,
      price: d.limit_price,
      portfolioId: d.portfolio_id,
    };
  }
  protected readonly ago = (v: string) => capitalise(formatAgo(v));
  protected readonly when = (v: string) => formatDateTime(v);
  protected readonly statusOf = (d: OrderDraftView) => draftStatus(d.status);
  protected readonly sourceOf = (d: OrderDraftView) => draftSource(d.source);

  /** Real money: the order's portfolio trades at a real broker. Brass is for this only. */
  protected isLive(d: OrderDraftView): boolean {
    return this.ctx.options().find((p) => p.id === d.portfolio_id)?.trading === 'live';
  }

  private portfolioName(d: OrderDraftView): string {
    return this.ctx.options().find((p) => p.id === d.portfolio_id)?.name ?? 'Your portfolio';
  }

  private currency(d: OrderDraftView): string {
    return this.ctx.options().find((p) => p.id === d.portfolio_id)?.base_currency ?? 'USD';
  }

  protected lines(d: OrderDraftView): TicketLine[] {
    const currency = this.currency(d);
    return [
      { label: 'Portfolio', value: this.portfolioName(d) },
      {
        label: 'Type',
        value:
          d.order_type === 'limit'
            ? `Limit at ${formatMoney(d.limit_price, { currency })}`
            : 'Market',
      },
      { label: 'Last close', value: formatMoney(d.reference_price, { currency }) },
      { label: 'About', value: formatMoney(d.notional, { currency }) },
    ];
  }

  private setProblem(id: string, text: string | null): void {
    this.problems.update((all) => {
      const next = { ...all };
      if (text) next[id] = text;
      else delete next[id];
      return next;
    });
  }

  protected async approve(d: OrderDraftView): Promise<void> {
    const live = this.isLive(d);
    const ok = await this.stepUp.ensure(`Approve the order for ${d.ticker}.`);
    if (!ok) return;
    const confirmed = await this.confirm.confirm({
      title: `${d.side === 'buy' ? 'Buy' : 'Sell'} ${formatNumber(d.quantity)} ${d.ticker}?`,
      message: live
        ? 'Approving places this order at your broker with real money, through every check.'
        : 'Approving places this order in your paper portfolio, through every check. No real money moves.',
      confirmLabel: 'Approve and place',
      tone: live ? 'danger' : 'default',
      typedConfirmation: live ? d.ticker : undefined,
      ticket: {
        side: d.side,
        live,
        lines: [{ label: 'Quantity', value: formatNumber(d.quantity) }, ...this.lines(d)],
      },
    });
    if (!confirmed) return;
    this.busy.set(d.id);
    this.setProblem(d.id, null);
    try {
      const { order } = await this.api.approve(d.id);
      this.toasts.success(
        order.status === 'filled'
          ? `Placed and filled: ${order.side} ${formatNumber(order.quantity)} ${order.ticker}.`
          : `Placed the order for ${formatNumber(order.quantity)} ${order.ticker}.`,
      );
    } catch (err) {
      const refused = refusalOf(err);
      if (refused) {
        this.setProblem(d.id, `Not placed: ${refused.message}`);
      } else if (!(err instanceof ApiError && err.code === 'step_up_required')) {
        this.setProblem(d.id, errorMessage(err));
      }
    } finally {
      this.busy.set(null);
      this.changed.emit();
    }
  }

  protected async reject(d: OrderDraftView): Promise<void> {
    const body = await this.dialog().open({
      title: `Reject the suggested order for ${d.ticker}?`,
      message: 'Nothing is placed. The assistant sees that you said no.',
      confirmLabel: 'Reject',
      tone: 'danger',
      minReason: 0,
      reasonHint: 'Optional. Kept with the suggested order.',
    });
    if (!body) return;
    this.busy.set(d.id);
    try {
      await this.api.reject(d.id, body.reason || undefined);
      this.toasts.success(`Rejected the suggested order for ${d.ticker}.`);
      this.changed.emit();
    } catch {
      // The error interceptor already showed the API's message.
    } finally {
      this.busy.set(null);
    }
  }
}

function capitalise(text: string): string {
  return text ? text.charAt(0).toUpperCase() + text.slice(1) : text;
}
