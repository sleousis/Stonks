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

import type { OrderDraftView } from '../../api/models';
import { OrderDraftsService } from '../../api/order-drafts.service';
import { SessionService } from '../../core/auth/session.service';
import { StepUpService } from '../../core/auth/step-up.service';
import { ConfirmService, type TicketLine } from '../../core/confirm/confirm.service';
import { formatAgo, formatDateTime, formatMoney, formatNumber } from '../../core/format/format';
import { ApiError, errorMessage } from '../../core/http/api-error';
import { ToastService } from '../../core/notify/toast.service';
import { PortfolioContextService } from '../../core/portfolio/portfolio-context.service';
import { autoRefresh } from '../../shared/auto-refresh';
import { ModeStamp } from '../../shared/ui/mode-stamp';
import { PermissionNote } from '../../shared/ui/permission-note';
import { type SegmentOption, Segmented } from '../../shared/ui/segmented';
import { SideTag } from '../../shared/ui/side-tag';
import { EmptyState, ErrorState, LoadingState } from '../../shared/ui/states';
import { StatusChangeDialog } from '../../shared/ui/status-change-dialog';
import { StatusPill, type PillTone } from '../../shared/ui/status-pill';
import { refusalOf } from './order-refusal';
import { TaxPreviewPanel, type TaxQuestion } from './tax-preview';

type DraftStatus = OrderDraftView['status'];
type Filter = DraftStatus | 'all';

const FILTERS: SegmentOption<Filter>[] = [
  { value: 'pending', label: 'Waiting' },
  { value: 'placed', label: 'Placed' },
  { value: 'rejected', label: 'Rejected' },
  { value: 'expired', label: 'Expired' },
  { value: 'all', label: 'All' },
];

const STATUS: Record<DraftStatus, { label: string; tone: PillTone }> = {
  pending: { label: 'Waiting for you', tone: 'progress' },
  placed: { label: 'Placed', tone: 'positive' },
  rejected: { label: 'Rejected', tone: 'negative' },
  expired: { label: 'Expired', tone: 'neutral' },
  cancelled: { label: 'Cancelled', tone: 'neutral' },
};

const SOURCES: Record<OrderDraftView['source'], string> = {
  assistant: 'The assistant',
  console: 'You, in the console',
  mcp: 'An agent tool',
};

export function draftStatus(status: DraftStatus): { label: string; tone: PillTone } {
  return STATUS[status] ?? { label: status, tone: 'neutral' };
}

export function draftSource(source: OrderDraftView['source']): string {
  return SOURCES[source] ?? source;
}

/**
 * Orders the assistant proposed, waiting for you. Nothing trades until you
 * approve one here: approving asks for a fresh code, shows the order ticket,
 * then places it as a manual order through every halt and risk rule. A
 * refused order leaves the draft rejected with the reason.
 */
@Component({
  selector: 'app-order-drafts-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    RouterLink,
    Segmented,
    SideTag,
    ModeStamp,
    StatusPill,
    PermissionNote,
    LoadingState,
    EmptyState,
    ErrorState,
    StatusChangeDialog,
    TaxPreviewPanel,
  ],
  template: `
    <section class="panel" aria-labelledby="drafts-title">
      <div class="panel-head">
        <h2 id="drafts-title">Order drafts</h2>
        <app-segmented label="Show drafts" [options]="filters" [(value)]="filter" />
      </div>
      <p class="lead">
        Orders the assistant proposed. Nothing trades until you approve one, with a fresh code from
        your authenticator app.
      </p>

      @if (drafts.error(); as err) {
        <app-error-state title="Could not load drafts" [error]="err" (retry)="drafts.reload()" />
      } @else if (!drafts.hasValue()) {
        <app-loading-state label="Loading drafts" [rows]="3" />
      } @else if (drafts.value().length === 0) {
        <app-empty-state
          [title]="filter() === 'pending' ? 'Nothing waiting for you' : 'No drafts here'"
          message="When the assistant proposes an order, it waits here for your approval."
        />
      } @else {
        <ul class="drafts">
          @for (d of drafts.value(); track d.id) {
            <li>
              <article
                class="ticket"
                [class.live]="isLive(d)"
                [attr.aria-labelledby]="'draft-' + d.id"
              >
                <p class="ticket-head">
                  <span class="ticket-kind" [id]="'draft-' + d.id">
                    {{ d.side === 'buy' ? 'Buy' : 'Sell' }} {{ num(d.quantity) }} {{ d.ticker }}
                  </span>
                  <app-side-tag [side]="d.side" />
                  <app-mode-stamp [live]="isLive(d)" />
                </p>
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
                    From {{ sourceOf(d) }}, {{ ago(d.created_at) }}.
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
                      Reject<span class="visually-hidden"> the draft for {{ d.ticker }}</span>
                    </button>
                    <button
                      type="button"
                      class="btn"
                      [class.btn-danger]="isLive(d)"
                      [class.btn-primary]="!isLive(d)"
                      [disabled]="busy() === d.id || !canApprove()"
                      (click)="approve(d)"
                    >
                      Approve and place<span class="visually-hidden">
                        the draft for {{ d.ticker }}</span
                      >
                    </button>
                  </div>
                }
              </article>
            </li>
          }
        </ul>
        <div class="note-row"><app-permission-note permission="orders.approve" /></div>
      }
    </section>
    <app-status-change-dialog />
  `,
  styles: `
    @use 'breakpoints' as bp;
    :host {
      display: block;
      min-width: 0;
    }
    .panel-head {
      flex-wrap: wrap;
      gap: var(--space-2);
    }
    .lead {
      padding: var(--space-3) var(--space-4) 0;
      color: var(--color-ink-2);
      font-size: var(--text-sm);
    }
    .drafts {
      display: grid;
      gap: var(--space-4);
      margin: 0;
      padding: var(--space-4);
      list-style: none;
      @include bp.from-desktop {
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }
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
      display: flex;
      flex-wrap: wrap;
      justify-content: flex-end;
      gap: var(--space-2);
    }
    .failure {
      padding: var(--space-2) var(--space-3);
      border-left: 3px solid var(--color-loss);
      background: var(--color-loss-soft);
      overflow-wrap: anywhere;
    }
    .note-row {
      padding: 0 var(--space-4) var(--space-3);
    }
  `,
})
export class OrderDraftsPage {
  private readonly api = inject(OrderDraftsService);
  private readonly ctx = inject(PortfolioContextService);
  private readonly session = inject(SessionService);
  private readonly stepUp = inject(StepUpService);
  private readonly confirm = inject(ConfirmService);
  private readonly toasts = inject(ToastService);
  private readonly dialog = viewChild.required(StatusChangeDialog);

  protected readonly filters = FILTERS;
  protected readonly filter = signal<Filter>('pending');
  protected readonly drafts = resource({
    params: () => ({ status: this.filter() }),
    loader: ({ params }) => this.api.list(params.status === 'all' ? undefined : params.status),
  });
  protected readonly auto = autoRefresh(() => [this.drafts]);
  protected readonly busy = signal<string | null>(null);
  /** The last failure per draft, shown on its ticket. */
  protected readonly problems = signal<Record<string, string>>({});
  protected readonly canApprove = computed(() => this.session.can('orders.approve'));
  protected readonly canReject = computed(() => this.session.can('portfolio.trade'));

  protected readonly num = (v: number) => formatNumber(v);

  /** The tax preview of a draft, asked before you approve it. */
  protected taxOf(d: OrderDraftView): TaxQuestion {
    return {
      ticker: d.ticker,
      side: d.side,
      quantity: d.quantity,
      price: d.limit_price,
      portfolioId: d.portfolio_id,
    };
  }
  protected readonly ago = (v: string) => formatAgo(v);
  protected readonly when = (v: string) => formatDateTime(v);
  protected readonly statusOf = (d: OrderDraftView) => draftStatus(d.status);
  protected readonly sourceOf = (d: OrderDraftView) => draftSource(d.source);

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
        : 'Approving places this order in your paper portfolio, through every check.',
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
      this.drafts.reload();
    }
  }

  protected async reject(d: OrderDraftView): Promise<void> {
    const body = await this.dialog().open({
      title: `Reject the draft for ${d.ticker}?`,
      message: 'Nothing is placed. The assistant sees that you said no.',
      confirmLabel: 'Reject draft',
      tone: 'danger',
      minReason: 0,
      reasonHint: 'Optional. Kept with the draft.',
    });
    if (!body) return;
    this.busy.set(d.id);
    try {
      await this.api.reject(d.id, body.reason || undefined);
      this.toasts.success(`Rejected the draft for ${d.ticker}.`);
      this.drafts.reload();
    } catch {
      // The error interceptor already showed the API's message.
    } finally {
      this.busy.set(null);
    }
  }
}
