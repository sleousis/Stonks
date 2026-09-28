import {
  ChangeDetectionStrategy,
  Component,
  computed,
  effect,
  inject,
  linkedSignal,
  resource,
  signal,
  viewChild,
} from '@angular/core';
import { RouterLink } from '@angular/router';

import type { OrderDraftView } from '../../api/models';
import { OrderDraftsService } from '../../api/order-drafts.service';
import { type TicketView, TicketsService } from '../../api/tickets.service';
import { SessionService } from '../../core/auth/session.service';
import { StepUpService } from '../../core/auth/step-up.service';
import { ConfirmService } from '../../core/confirm/confirm.service';
import { formatDate, formatDateTime, formatMoney, formatNumber } from '../../core/format/format';
import { ToastService } from '../../core/notify/toast.service';
import { PortfolioContextService } from '../../core/portfolio/portfolio-context.service';
import { TicketCountService } from '../../core/tickets/ticket-count.service';
import { UpdatedAgo, autoRefresh } from '../../shared/auto-refresh';
import { rowStrategyName } from '../../shared/strategy-names';
import { DataTable, TableCell, type TableColumn } from '../../shared/ui/data-table/data-table';
import { HelpTip } from '../../shared/ui/help-tip';
import { ModeStamp } from '../../shared/ui/mode-stamp';
import { PageHeader } from '../../shared/ui/page-header';
import { PermissionNote } from '../../shared/ui/permission-note';
import { SideTag } from '../../shared/ui/side-tag';
import { EmptyState, ErrorState, LoadingState } from '../../shared/ui/states';
import { type PageTab, PageTabs } from '../../shared/ui/page-tabs';
import { StatusPill } from '../../shared/ui/status-pill';
import { RejectSheet } from './reject-sheet';
import { SuggestedOrders } from './suggested-orders';
import { deciderWords, holdWords, ruleLines, ticketStatusLook } from './ticket-words';

type View = 'waiting' | 'history';

/** The tickets of one book and one trading run, approved together. */
interface Group {
  key: string;
  portfolioId: string;
  portfolioName: string;
  asOf: string;
  expiresAt: string;
  live: boolean;
  /** The portfolio's stage, for the stamp's words (BROKER PAPER). */
  stage: string | null;
  tickets: TicketView[];
  notional: number;
}

/**
 * Approvals (roadmap 19.8, F9): the one inbox for orders that wait for the
 * trader. Strategy tickets are the orders a book at a broker decided after
 * the close, as order tickets (side, ticker, quantity, limit, notional, why,
 * the rules that touched it) with Approve and Reject; Approve all takes one
 * code for the whole run. Suggested orders are the assistant's proposals.
 * History lists both and what became of them. Brass means real money.
 * The old Orders > Drafts address redirects here.
 */
@Component({
  selector: 'app-tickets-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    PageTabs,
    RouterLink,
    PageHeader,
    UpdatedAgo,
    SideTag,
    ModeStamp,
    StatusPill,
    HelpTip,
    PermissionNote,
    DataTable,
    TableCell,
    EmptyState,
    ErrorState,
    LoadingState,
    RejectSheet,
    SuggestedOrders,
  ],
  template: `
    <app-page-header
      title="Approvals"
      description="Orders waiting for your OK: trades from strategies you follow with Approve each trade, and orders the assistant suggested. Nothing goes out until you approve it."
    >
      <app-updated-ago [at]="auto.updatedAt()" />
    </app-page-header>

    <app-page-tabs
      #tabBar
      idPrefix="approvals"
      label="Approvals views"
      [tabs]="tabs()"
      [selected]="view()"
      (selectedChange)="pickView($event)"
    />

    <div
      class="view"
      role="tabpanel"
      [id]="tabBar.panelId(view())"
      [attr.aria-labelledby]="tabBar.tabId(view())"
    >
      @if (list.error(); as err) {
        <app-error-state
          title="Could not load your tickets"
          [error]="err"
          (retry)="list.reload()"
        />
      } @else if (!list.hasValue()) {
        <app-loading-state label="Loading your tickets" [rows]="3" />
      } @else if (view() === 'waiting') {
        @if (waitingCount() === 0) {
          <app-empty-state
            title="Nothing waits for you"
            message="Orders appear here after a trading run when a strategy you follow uses Approve each trade, and when the assistant suggests an order."
          >
            <a routerLink="/" class="btn">Back to Today</a>
          </app-empty-state>
        } @else {
          <p class="lead">
            {{ waitingCount() }} {{ waitingCount() === 1 ? 'order waits' : 'orders wait' }} for you.
            Approving asks for a code from your authenticator app.
            <app-help-tip term="Approve each trade" />
          </p>
          @if (groups().length) {
            <section class="source-group" aria-labelledby="from-strategies">
              <h2 id="from-strategies" class="source-title">
                From strategies you follow
                <span class="muted">{{ waiting().length }}</span>
              </h2>
              <p class="muted">
                Each ticket goes to your broker in the window before the next open.
              </p>
              @for (group of groups(); track group.key) {
                <section class="run" [attr.aria-labelledby]="'run-' + group.key">
                  <div class="run-head">
                    <div class="run-title">
                      <h3 [id]="'run-' + group.key">{{ group.portfolioName }}</h3>
                      <p class="muted">
                        Decided {{ date(group.asOf) }}. Send by
                        <span class="num">{{ dateTime(group.expiresAt) }}</span
                        >, unapproved tickets then expire.
                      </p>
                    </div>
                    @if (group.tickets.length > 1) {
                      <button
                        type="button"
                        class="btn btn-primary"
                        [disabled]="!canApprove() || busyGroup() === group.key"
                        (click)="approveAll(group)"
                      >
                        Approve all {{ group.tickets.length }}
                      </button>
                    }
                  </div>
                  <ul class="tickets">
                    @for (t of group.tickets; track t.id) {
                      <li>
                        <article
                          class="ticket"
                          [class.live]="group.live"
                          [attr.aria-labelledby]="'ticket-' + t.id"
                        >
                          <p class="ticket-head">
                            <app-side-tag [side]="t.side" />
                            <span class="ticket-kind" [id]="'ticket-' + t.id">
                              <span class="verb">{{ t.side === 'buy' ? 'Buy' : 'Sell' }}</span>
                              <span class="num">{{ qty(t.quantity) }}</span>
                              <strong class="num">{{ t.ticker }}</strong>
                            </span>
                            <app-mode-stamp [live]="group.live" [stage]="group.stage" />
                          </p>
                          @if (hold(t); as why) {
                            <p class="hold" [class.alarm]="t.hold === 'runaway'">{{ why }}</p>
                          }
                          <dl class="ticket-lines">
                            <div>
                              <dt>Price</dt>
                              <dd>
                                {{
                                  t.limit_price ? 'Limit ' + money(t.limit_price) : 'At the open'
                                }}
                              </dd>
                            </div>
                            <div>
                              <dt>Decided at</dt>
                              <dd>{{ money(t.reference_price) }}</dd>
                            </div>
                            <div>
                              <dt>About</dt>
                              <dd>{{ money(t.notional) }}</dd>
                            </div>
                            @if (t.strategy_id) {
                              <div>
                                <dt>Strategy</dt>
                                <dd>{{ strategyName(t) }}</dd>
                              </div>
                            }
                            @if (scoreText(t); as s) {
                              <div>
                                <dt>Signal score <app-help-tip term="Signal score" /></dt>
                                <dd>{{ s }}</dd>
                              </div>
                            }
                            @if (commission(t); as fee) {
                              <div>
                                <dt>Commission</dt>
                                <dd>{{ money(fee) }}</dd>
                              </div>
                            }
                          </dl>
                          @if (rules(t); as lines) {
                            @if (lines.length) {
                              <details class="rules">
                                <summary>
                                  Checked by {{ lines.length }} rule{{
                                    lines.length === 1 ? '' : 's'
                                  }}
                                </summary>
                                <ul>
                                  @for (line of lines; track $index) {
                                    <li>{{ line }}</li>
                                  }
                                </ul>
                              </details>
                            }
                          }
                          <div class="actions">
                            <button
                              type="button"
                              class="btn"
                              [disabled]="!canReject() || busy().has(t.id)"
                              (click)="reject(t)"
                            >
                              Reject
                            </button>
                            <button
                              type="button"
                              class="btn btn-primary"
                              [disabled]="!canApprove() || busy().has(t.id)"
                              (click)="approve(group, [t])"
                            >
                              Approve
                            </button>
                          </div>
                        </article>
                      </li>
                    }
                  </ul>
                </section>
              }
            </section>
          }
          @if (pendingDrafts().length) {
            <section class="source-group" aria-labelledby="suggested">
              <h2 id="suggested" class="source-title">
                Suggested orders <span class="muted">{{ pendingDrafts().length }}</span>
              </h2>
              <p class="muted">
                Orders the assistant proposed. Approving places one through every check, like an
                order you place by hand.
              </p>
              <app-suggested-orders [drafts]="pendingDrafts()" (changed)="drafts.reload()" />
            </section>
          }
          <app-permission-note permission="orders.approve" />
        }
      } @else {
        <section class="source-group" aria-labelledby="history-strategies">
          <h2 id="history-strategies" class="source-title">From strategies you follow</h2>
          @if (history().length === 0) {
            <app-empty-state
              title="No tickets yet"
              message="Every order a strategy you follow with Approve each trade decides is kept here, with what became of it."
            />
          } @else {
            <app-data-table
              caption="Your order tickets, newest first"
              [rows]="history()"
              [columns]="columns"
              [rowKey]="key"
            >
              <ng-template appCell="ticker" [appCellOf]="history()" let-t>
                <span class="order"
                  ><app-side-tag [side]="t.side" /> <span class="num">{{ t.ticker }}</span></span
                >
              </ng-template>
              <ng-template appCell="status" [appCellOf]="history()" let-t>
                <app-status-pill
                  [status]="t.status"
                  [label]="look(t.status).label"
                  [tone]="look(t.status).tone"
                  [form]="look(t.status).form"
                />
              </ng-template>
            </app-data-table>
          }
        </section>
        @if (pastDrafts().length) {
          <section class="source-group" aria-labelledby="history-suggested">
            <h2 id="history-suggested" class="source-title">Suggested orders</h2>
            <app-suggested-orders [drafts]="pastDrafts()" (changed)="drafts.reload()" />
          </section>
        }
      }
    </div>
    <app-reject-sheet />
  `,
  styles: `
    @use 'breakpoints' as bp;

    :host {
      display: grid;
      gap: var(--space-4);
      min-width: 0;
    }
    .lead {
      color: var(--color-ink-2);
    }
    .view {
      display: grid;
      gap: var(--space-5);
      min-width: 0;
    }
    .source-group {
      display: grid;
      gap: var(--space-3);
      min-width: 0;
    }
    .source-title {
      display: flex;
      align-items: baseline;
      gap: var(--space-2);
      font-size: var(--text-lg);
    }
    .source-title .muted {
      font-size: var(--text-sm);
      font-weight: var(--weight-regular, 400);
    }
    .run {
      display: grid;
      gap: var(--space-3);
      min-width: 0;
    }
    .run-head {
      display: flex;
      flex-wrap: wrap;
      align-items: flex-end;
      justify-content: space-between;
      gap: var(--space-2) var(--space-4);
    }
    .run-title {
      min-width: 0;
      flex: 1 1 16rem;
    }
    .run-title h3 {
      font-size: var(--text-lg);
      overflow-wrap: anywhere;
    }
    .muted {
      color: var(--color-ink-2);
      font-size: var(--text-sm);
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
    .hold {
      padding: var(--space-2) var(--space-3);
      border-left: 3px solid var(--color-info);
      border-radius: var(--radius-sm);
      background: var(--color-info-soft);
      font-size: var(--text-sm);
    }
    .hold.alarm {
      border-left-color: var(--color-warn);
      background: var(--color-warn-soft);
    }
    .rules summary {
      display: flex;
      align-items: center;
      min-height: var(--touch-min);
      color: var(--color-ink-2);
      font-size: var(--text-sm);
      cursor: pointer;
    }
    .rules ul {
      margin: 0 0 var(--space-2);
      padding-left: var(--space-4);
      font-size: var(--text-sm);
      overflow-wrap: anywhere;
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
    .run-head .btn {
      min-height: var(--touch-min);

      @include bp.phone {
        width: 100%;
        justify-content: center;
      }
    }
    .order {
      display: inline-flex;
      align-items: center;
      gap: var(--space-2);
    }
  `,
})
export class TicketsPage {
  private readonly api = inject(TicketsService);
  private readonly session = inject(SessionService);
  private readonly stepUp = inject(StepUpService);
  private readonly confirm = inject(ConfirmService);
  private readonly toasts = inject(ToastService);
  private readonly portfolios = inject(PortfolioContextService);
  private readonly rejectSheet = viewChild.required(RejectSheet);
  private readonly ticketCount = inject(TicketCountService);

  private readonly draftsApi = inject(OrderDraftsService);

  protected readonly views: readonly { value: View; label: string }[] = [
    { value: 'waiting', label: 'Waiting for you' },
    { value: 'history', label: 'History' },
  ];
  protected readonly view = signal<View>('waiting');
  /** The tab bar, with the count of orders waiting beside its tab. */
  protected readonly tabs = computed<PageTab[]>(() =>
    this.views.map((v) => ({
      id: v.value,
      label: v.label,
      badge: v.value === 'waiting' ? this.waitingCount() : null,
    })),
  );
  /** Approving needs a signed-in browser with a fresh code. */
  protected readonly canApprove = computed(() => this.session.can('orders.approve'));
  protected readonly canReject = computed(() => this.session.can('portfolio.trade'));
  /** Tickets with a decision in flight. */
  protected readonly busy = signal<ReadonlySet<string>>(new Set());
  protected readonly busyGroup = signal<string | null>(null);

  protected readonly list = resource({ loader: () => this.api.list() });
  /** Suggested orders (every status): pending ones wait here, the rest are history. */
  protected readonly drafts = resource({ loader: () => this.draftsApi.list() });
  protected readonly auto = autoRefresh(() => [this.list, this.drafts]);
  private readonly draftItems = computed<OrderDraftView[]>(() =>
    this.drafts.hasValue() ? this.drafts.value() : [],
  );
  protected readonly pendingDrafts = computed(() =>
    this.draftItems().filter((d) => d.status === 'pending'),
  );
  protected readonly pastDrafts = computed(() =>
    this.draftItems().filter((d) => d.status !== 'pending'),
  );
  /** The list as shown, updated in place from each decision's response. */
  private readonly items = linkedSignal(() => (this.list.hasValue() ? this.list.value() : []));

  protected readonly waiting = computed(() =>
    this.items().filter((t) => t.status === 'awaiting_approval'),
  );
  protected readonly history = computed(() =>
    this.items().filter((t) => t.status !== 'awaiting_approval'),
  );
  /** Strategy tickets and suggested orders that wait, together (F9). */
  protected readonly waitingCount = computed(
    () => this.waiting().length + this.pendingDrafts().length,
  );
  /** The nav badge follows what this page shows (22.10). */
  private readonly badge = effect(() => {
    if (this.list.hasValue()) {
      this.ticketCount.set(this.waiting().length, this.pendingDrafts().length);
    }
  });
  protected readonly groups = computed<Group[]>(() => {
    const byKey = new Map<string, Group>();
    for (const t of this.waiting()) {
      const key = `${t.portfolio_id}-${t.as_of}`;
      let group = byKey.get(key);
      if (!group) {
        group = {
          key,
          portfolioId: t.portfolio_id,
          portfolioName: t.portfolio_name,
          asOf: t.as_of,
          expiresAt: t.expires_at,
          live: this.isLive(t.portfolio_id),
          stage: this.stageOf(t.portfolio_id),
          tickets: [],
          notional: 0,
        };
        byKey.set(key, group);
      }
      group.tickets.push(t);
      group.notional += t.notional ?? 0;
    }
    return [...byKey.values()];
  });

  protected readonly columns: readonly TableColumn<TicketView>[] = [
    { key: 'ticker', label: 'Order', mobile: 'title', sortable: false },
    { key: 'portfolio_name', label: 'Portfolio' },
    { key: 'quantity', label: 'Quantity', format: 'number' },
    { key: 'notional', label: 'About', format: 'money' },
    { key: 'status', label: 'Status', value: (t) => ticketStatusLook(t.status).label },
    { key: 'decided_by', label: 'Decided by', value: (t) => deciderWords(t.decided_by) },
    { key: 'as_of', label: 'Decided', format: 'date' },
  ];
  protected readonly key = (t: TicketView) => t.id;
  protected readonly look = ticketStatusLook;
  protected readonly hold = (t: TicketView) => holdWords(t.hold);
  protected readonly rules = ruleLines;
  protected readonly strategyName = (t: TicketView) => rowStrategyName(t);
  protected readonly money = (v: number | null | undefined) => formatMoney(v);
  protected readonly qty = (v: number) => formatNumber(v);
  protected readonly date = formatDate;
  protected readonly dateTime = formatDateTime;

  /** The broker's commission estimate from its what-if preview, when it gave one. */
  protected commission(t: TicketView): number | null {
    const value = t.what_if ? t.what_if['commission'] : null;
    return typeof value === 'number' ? value : null;
  }

  /** The strategy's score for the ticker when it decided, two decimals. */
  protected scoreText(t: TicketView): string | null {
    const value = t.reason['score'];
    return typeof value === 'number' ? formatNumber(value, { digits: 2 }) : null;
  }

  /** A tab picked in the tab bar (arrow keys included, the WAI-ARIA tabs pattern). */
  protected pickView(value: string | null): void {
    if (value === 'waiting' || value === 'history') this.view.set(value);
  }

  protected async approve(group: Group, tickets: readonly TicketView[]): Promise<void> {
    const ids = tickets.map((t) => t.id);
    if (ids.some((id) => this.busy().has(id))) return;
    const what =
      tickets.length === 1
        ? `${tickets[0].side === 'buy' ? 'Buy' : 'Sell'} ${tickets[0].ticker}`
        : `${tickets.length} orders in ${group.portfolioName}`;
    if (!(await this.stepUp.ensure(`Approve ${what}.`))) return;
    this.mark(ids, true);
    try {
      const done = await this.api.approve(ids);
      this.replace(done.items);
      this.toasts.success(
        tickets.length === 1
          ? `${what} is approved. It goes to your broker before the open.`
          : `${what} are approved. They go to your broker before the open.`,
      );
    } catch {
      // The error interceptor showed the API's message (expired, decided).
      this.list.reload();
    } finally {
      this.mark(ids, false);
    }
  }

  /** One ticket for the whole run, then one code (roadmap 19.8). */
  protected async approveAll(group: Group): Promise<void> {
    const ok = await this.confirm.confirm({
      title: `Approve ${group.tickets.length} orders in ${group.portfolioName}?`,
      message:
        'Each order goes to your broker in the window before the next open. ' +
        'You can still stop trading from any page until then.',
      confirmLabel: `Approve ${group.tickets.length}`,
      ticket: {
        kind: 'Approve a trading run',
        live: group.live,
        lines: [
          { label: 'Portfolio', value: group.portfolioName },
          { label: 'Orders', value: String(group.tickets.length) },
          { label: 'About', value: formatMoney(group.notional) },
          { label: 'Send by', value: formatDateTime(group.expiresAt) },
        ],
      },
    });
    if (!ok) return;
    this.busyGroup.set(group.key);
    try {
      await this.approve(group, group.tickets);
    } finally {
      this.busyGroup.set(null);
    }
  }

  protected async reject(ticket: TicketView): Promise<void> {
    const what = `${ticket.side === 'buy' ? 'Buy' : 'Sell'} ${ticket.ticker}`;
    const reason = await this.rejectSheet().open(what);
    if (reason === null) return;
    this.mark([ticket.id], true);
    try {
      const done = await this.api.reject(ticket.id, reason);
      this.replace([done]);
      this.toasts.success(`${what} is rejected. Nothing goes to your broker for it.`);
    } catch {
      this.list.reload();
    } finally {
      this.mark([ticket.id], false);
    }
  }

  private stageOf(portfolioId: string): string | null {
    return this.portfolios.options().find((p) => p.id === portfolioId)?.live_stage ?? null;
  }

  private isLive(portfolioId: string): boolean {
    const book = this.portfolios.options().find((p) => p.id === portfolioId);
    return book ? book.trading === 'live' : true;
  }

  private replace(updated: readonly TicketView[]): void {
    const byId = new Map(updated.map((t) => [t.id, t]));
    this.items.update((list) => list.map((t) => byId.get(t.id) ?? t));
  }

  private mark(ids: readonly string[], on: boolean): void {
    this.busy.update((current) => {
      const next = new Set(current);
      for (const id of ids) {
        if (on) next.add(id);
        else next.delete(id);
      }
      return next;
    });
  }
}
