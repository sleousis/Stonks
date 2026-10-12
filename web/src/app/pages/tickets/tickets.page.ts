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
      description="Orders that wait for your OK, from strategies and the assistant. Nothing goes out until you approve it."
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
          <p class="standfirst lead">
            {{ waitingCount() }} {{ waitingCount() === 1 ? 'order waits' : 'orders wait' }} for you.
            Approving asks for a code from your authenticator app.
            <app-help-tip term="Approve each trade" />
          </p>
          @if (groups().length) {
            <section class="page-section source-group" aria-labelledby="from-strategies">
              <header class="section-head">
                <h2 id="from-strategies">
                  From strategies you follow
                  <span class="count">{{ waiting().length }}</span>
                </h2>
                <span class="head-note">Each ticket goes to your broker before the next open.</span>
              </header>
              @for (group of groups(); track group.key) {
                <section class="run" [attr.aria-labelledby]="'run-' + group.key">
                  <div class="run-head">
                    <label class="check run-check">
                      <input
                        type="checkbox"
                        [checked]="allPicked(group)"
                        [indeterminate]="somePicked(group) && !allPicked(group)"
                        [attr.aria-label]="'Select every order in ' + group.portfolioName"
                        (change)="pickGroup(group, $any($event.target).checked)"
                      />
                    </label>
                    <div class="run-title">
                      <h3 [id]="'run-' + group.key">
                        {{ group.portfolioName }}
                        <app-mode-stamp size="sm" [live]="group.live" [stage]="group.stage" />
                      </h3>
                      <p class="meta">
                        Decided {{ date(group.asOf) }}. Send by
                        <span class="num">{{ dateTime(group.expiresAt) }}</span
                        >, unapproved tickets then expire.
                      </p>
                    </div>
                    @if (group.tickets.length > 1) {
                      <button
                        type="button"
                        class="btn btn-compact"
                        [class.btn-live]="group.live"
                        [class.btn-primary]="!group.live"
                        [disabled]="!canApprove() || busyGroup() === group.key"
                        (click)="approveAll(group)"
                      >
                        Approve all {{ group.tickets.length }}
                      </button>
                    }
                  </div>
                  <div class="inbox-head" aria-hidden="true">
                    <span></span>
                    <span>Order</span>
                    <span class="end">Price</span>
                    <span class="end">About</span>
                    <span></span>
                  </div>
                  <ul class="tickets">
                    @for (t of group.tickets; track t.id) {
                      <li>
                        <article
                          class="ticket"
                          [class.live]="group.live"
                          [class.picked]="picked().has(t.id)"
                          [attr.aria-labelledby]="'ticket-' + t.id"
                        >
                          <label class="check pick">
                            <input
                              type="checkbox"
                              [checked]="picked().has(t.id)"
                              [attr.aria-label]="
                                'Select ' + (t.side === 'buy' ? 'buy ' : 'sell ') + t.ticker
                              "
                              (change)="pick(t, $any($event.target).checked)"
                            />
                          </label>
                          <div class="order">
                            <p class="order-line">
                              <app-side-tag [side]="t.side" />
                              <span class="ticket-kind" [id]="'ticket-' + t.id">
                                <span class="verb">{{ t.side === 'buy' ? 'Buy' : 'Sell' }}</span>
                                <span class="num">{{ qty(t.quantity) }}</span>
                                <strong>{{ t.ticker }}</strong>
                              </span>
                              <app-mode-stamp
                                class="row-stamp"
                                size="sm"
                                [live]="group.live"
                                [stage]="group.stage"
                              />
                            </p>
                            <p class="meta">
                              @if (t.strategy_id) {
                                <span>{{ strategyName(t) }}</span>
                              }
                              <span
                                >Decided at <span class="num">{{ money(t.reference_price, t) }}</span></span
                              >
                              @if (scoreText(t); as s) {
                                <span
                                  >Signal score <span class="num">{{ s }}</span>
                                  <app-help-tip term="Signal score"
                                /></span>
                              }
                              @if (commission(t); as fee) {
                                <span
                                  >Commission <span class="num">{{ money(fee, t) }}</span></span
                                >
                              }
                            </p>
                            @if (hold(t); as why) {
                              @if (t.hold === 'runaway') {
                                <p class="hold alarm callout" data-tone="warn">{{ why }}</p>
                              } @else {
                                <p class="hold">{{ why }}</p>
                              }
                            }
                            @if (rules(t); as lines) {
                              @if (lines.length) {
                                <details class="rules">
                                  <summary>
                                    <span class="disclosure-chev" aria-hidden="true"></span>
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
                          </div>
                          <p class="price num">
                            <span class="cell-label">Price</span>
                            {{ t.limit_price ? 'Limit ' + money(t.limit_price, t) : 'At the open' }}
                          </p>
                          <p class="about num" [class.brass]="group.live">
                            <span class="cell-label">About</span>
                            {{ money(t.notional, t) }}
                          </p>
                          <div class="actions">
                            <button
                              type="button"
                              class="btn btn-compact btn-ghost"
                              [disabled]="!canReject() || busy().has(t.id)"
                              (click)="reject(t)"
                            >
                              Reject
                            </button>
                            <button
                              type="button"
                              class="btn btn-compact"
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
            <section class="page-section source-group" aria-labelledby="suggested">
              <header class="section-head">
                <h2 id="suggested">
                  Suggested orders <span class="count">{{ pendingDrafts().length }}</span>
                </h2>
              </header>
              <p class="head-note">
                Orders the assistant proposed. Approving places one through every check, like an
                order you place by hand.
              </p>
              <app-suggested-orders [drafts]="pendingDrafts()" (changed)="drafts.reload()" />
            </section>
          }
          <app-permission-note permission="orders.approve" />
        }
      } @else {
        <section class="page-section source-group" aria-labelledby="history-strategies">
          <header class="section-head">
            <h2 id="history-strategies">From strategies you follow</h2>
          </header>
          @if (history().length === 0) {
            <app-empty-state
              title="No tickets yet"
              message="Every order a strategy you follow with Approve each trade decides is kept here, with what became of it."
            />
          } @else {
            <div>
              <app-data-table
                caption="Your order tickets, newest first"
                [rows]="history()"
                [columns]="columns"
                [rowKey]="key"
              >
                <ng-template appCell="ticker" [appCellOf]="history()" let-t>
                  <span class="order-cell"
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
            </div>
          }
        </section>
        @if (pastDrafts().length) {
          <section class="page-section source-group" aria-labelledby="history-suggested">
            <header class="section-head">
              <h2 id="history-suggested">Suggested orders</h2>
            </header>
            <app-suggested-orders [drafts]="pastDrafts()" (changed)="drafts.reload()" />
          </section>
        }
      }
    </div>
    @if (view() === 'waiting' && pickedTickets().length) {
      <div class="action-bar" role="region" aria-label="Selected orders">
        <p class="picked-count">
          <span class="count-badge">{{ pickedTickets().length }}</span>
          selected, about <span class="num">{{ pickedNotional() }}</span>
        </p>
        <button type="button" class="btn btn-ghost" (click)="picked.set(emptySet)">Clear</button>
        <button
          type="button"
          class="btn btn-danger"
          [disabled]="!canReject() || bulkBusy()"
          (click)="rejectPicked()"
        >
          Reject {{ pickedTickets().length }}
        </button>
        <button
          type="button"
          class="btn"
          [class.btn-live]="pickedLive()"
          [class.btn-primary]="!pickedLive()"
          [disabled]="!canApprove() || bulkBusy()"
          (click)="approvePicked()"
        >
          Approve {{ pickedTickets().length }}
        </button>
      </div>
    }
    <app-reject-sheet />
  `,
  styles: `
    @use 'breakpoints' as bp;

    :host {
      display: block;
      min-width: 0;
    }
    .view {
      display: grid;
      gap: var(--section-gap);
      min-width: 0;
      margin-top: var(--space-6);
    }
    .lead {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: var(--space-1) var(--space-2);
    }
    .source-group {
      display: grid;
      gap: 0;
      min-width: 0;
    }
    .head-note {
      color: var(--muted);
      font-size: var(--text-sm);
    }
    p.head-note {
      padding-top: var(--space-3);
    }
    /* A run: its portfolio, the deadline and Approve all, then the rows. */
    .run {
      min-width: 0;
    }
    .run-head {
      display: grid;
      grid-template-columns: 28px minmax(0, 1fr) auto;
      align-items: center;
      gap: var(--space-2) var(--space-3);
      padding: var(--space-4) 0 var(--space-3);
    }
    .run-title h3 {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: var(--space-2);
      font-size: var(--text-body);
      font-weight: var(--weight-semibold);
      overflow-wrap: anywhere;
    }
    .meta {
      display: flex;
      flex-wrap: wrap;
      gap: 2px var(--space-3);
      color: var(--muted);
      font-size: var(--text-sm);
    }
    .run-title .meta {
      margin-top: 2px;
    }
    .check {
      min-height: 0;
    }
    /* The inbox: a header row, then one row per ticket with 1px rules. */
    .inbox-head,
    .ticket {
      display: grid;
      grid-template-columns: 28px minmax(0, 1fr) 9rem 8rem auto;
      gap: var(--space-3);
      align-items: start;
    }
    .inbox-head {
      align-items: center;
      height: 36px;
      border-bottom: 1px solid var(--rule);
      color: var(--muted);
      font-size: var(--text-xs);
      font-weight: var(--weight-medium);
    }
    .end {
      text-align: right;
    }
    .tickets {
      margin: 0;
      padding: 0;
      list-style: none;
    }
    /* A row, not a box: override the global ticket look. */
    .tickets .ticket {
      position: static;
      padding: 14px 0;
      border: 0;
      border-bottom: 1px solid var(--rule);
      border-radius: 0;
      background: none;
      transition: background-color var(--dur-fast) var(--ease-out);
    }
    .tickets .ticket:hover {
      background: var(--sunk);
    }
    .tickets .ticket.picked {
      background: var(--accent-soft);
    }
    .pick {
      padding-top: 1px;
    }
    .order {
      display: grid;
      gap: var(--space-1);
      min-width: 0;
    }
    .order-line {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: var(--space-2);
    }
    .ticket-kind {
      display: inline-flex;
      flex-wrap: wrap;
      align-items: baseline;
      gap: var(--space-1) var(--space-2);
      font-size: var(--text-body);
      min-width: 0;
      overflow-wrap: anywhere;
    }
    .ticket-kind strong {
      font-weight: var(--weight-bold);
    }
    .verb {
      color: var(--ink-2);
    }
    .price,
    .about {
      padding-top: 1px;
      font-size: var(--text-md);
      text-align: right;
      white-space: normal;
    }
    .about {
      font-weight: var(--weight-semibold);
    }
    .cell-label {
      display: none;
    }
    .hold {
      max-width: 68ch;
      color: var(--ink-2);
      font-size: var(--text-sm);
      overflow-wrap: anywhere;
    }
    .rules summary {
      display: inline-flex;
      align-items: center;
      gap: var(--space-1);
      min-height: 28px;
      color: var(--ink-2);
      font-size: var(--text-sm);
      cursor: pointer;
      list-style: none;
    }
    .rules summary::-webkit-details-marker {
      display: none;
    }
    .rules ul {
      margin: 0 0 var(--space-2);
      padding-left: var(--space-5);
      color: var(--ink-2);
      font-size: var(--text-sm);
      overflow-wrap: anywhere;
    }
    .actions {
      display: flex;
      gap: var(--space-1);
      justify-content: flex-end;
    }
    /* The bar for ticked rows: fixed at the foot, above the bottom nav. */
    .action-bar {
      position: sticky;
      bottom: var(--space-4);
      z-index: var(--z-sticky);
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: var(--space-2);
      margin-top: var(--space-6);
      padding: var(--space-3) var(--space-3) var(--space-3) var(--space-4);
      border-radius: var(--r-sheet);
      background: var(--chrome);
      color: var(--chrome-ink);
      box-shadow: var(--shadow-overlay);
      animation: menu-in var(--dur-menu) var(--ease-out);
    }
    .picked-count {
      display: inline-flex;
      align-items: center;
      gap: var(--space-2);
      margin-right: auto;
      font-size: var(--text-md);
    }
    .picked-count .count-badge {
      background: var(--chrome-ink);
      color: var(--chrome);
    }
    .action-bar .btn-ghost {
      color: var(--chrome-muted);
    }
    .action-bar .btn-ghost:hover:not(:disabled) {
      background: var(--chrome-2);
      color: var(--chrome-ink);
    }
    .action-bar .btn-danger {
      border-color: var(--chrome-stop-line);
      background: transparent;
      color: var(--chrome-stop);
    }
    .action-bar .btn-danger:hover:not(:disabled) {
      background: var(--chrome-2);
    }
    .action-bar .btn-primary {
      border-color: var(--chrome-accent);
      background: var(--chrome-accent);
      color: var(--chrome);
    }
    .order-cell {
      display: inline-flex;
      align-items: center;
      gap: var(--space-2);
    }
    /* Tablet: price and about stack in one column. */
    @media (max-width: 1099.98px) {
      .inbox-head,
      .ticket {
        grid-template-columns: 28px minmax(0, 1fr) 8rem auto;
      }
      .inbox-head > :nth-child(3) {
        display: none;
      }
      .price {
        grid-column: 3;
        grid-row: 2;
        padding-top: 0;
        color: var(--muted);
        font-size: var(--text-sm);
      }
      .about {
        grid-column: 3;
        grid-row: 1;
      }
      .actions {
        grid-column: 4;
        grid-row: 1;
      }
    }
    /* Phones: list items. Name and meta on the left, about on the right,
       the actions on their own line. */
    @include bp.phone {
      .inbox-head {
        display: none;
      }
      .run-head {
        grid-template-columns: 44px minmax(0, 1fr);
      }
      .run-head .btn {
        grid-column: 1 / -1;
        width: 100%;
      }
      .tickets .ticket {
        grid-template-columns: 44px minmax(0, 1fr) auto;
        row-gap: var(--space-2);
      }
      .check {
        min-height: var(--touch-min);
        min-width: var(--touch-min);
        align-items: flex-start;
        padding-top: 2px;
      }
      .order {
        grid-column: 2 / -1;
        grid-row: 1;
      }
      .row-stamp {
        display: none;
      }
      .about {
        grid-column: 3;
        grid-row: 2;
        font-size: var(--text-lead);
      }
      .price {
        grid-column: 2;
        grid-row: 2;
        text-align: left;
      }
      .actions {
        grid-column: 2 / -1;
        grid-row: 3;
        display: grid;
        grid-template-columns: 1fr 1fr;
      }
      .actions .btn {
        min-height: var(--touch-min);
      }
      .action-bar {
        bottom: calc(var(--bottom-nav-h) + env(safe-area-inset-bottom) + var(--space-2));
      }
      .action-bar .picked-count {
        flex-basis: 100%;
      }
      .action-bar .btn {
        flex: 1;
      }
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

  /** Tickets ticked in the inbox, for the action bar at the foot. */
  protected readonly emptySet: ReadonlySet<string> = new Set();
  protected readonly picked = signal<ReadonlySet<string>>(this.emptySet);
  protected readonly bulkBusy = signal(false);
  protected readonly pickedTickets = computed(() =>
    this.waiting().filter((t) => this.picked().has(t.id)),
  );
  protected readonly pickedLive = computed(() =>
    this.pickedTickets().some((t) => this.isLive(t.portfolio_id)),
  );
  protected readonly pickedNotional = computed(() => {
    const ts = this.pickedTickets();
    const total = ts.reduce((sum, t) => sum + (t.notional ?? 0), 0);
    return this.money(total, ts[0]);
  });

  protected pick(t: TicketView, on: boolean): void {
    this.picked.update((current) => {
      const next = new Set(current);
      if (on) next.add(t.id);
      else next.delete(t.id);
      return next;
    });
  }

  protected pickGroup(group: Group, on: boolean): void {
    for (const t of group.tickets) this.pick(t, on);
  }

  protected allPicked(group: Group): boolean {
    return group.tickets.every((t) => this.picked().has(t.id));
  }

  protected somePicked(group: Group): boolean {
    return group.tickets.some((t) => this.picked().has(t.id));
  }

  /** Approve the ticked tickets: one ticket to confirm, then each run's approval. */
  protected async approvePicked(): Promise<void> {
    const picked = this.pickedTickets();
    if (!picked.length) return;
    const groups = this.groups()
      .map((g) => ({ group: g, tickets: g.tickets.filter((t) => this.picked().has(t.id)) }))
      .filter((g) => g.tickets.length);
    if (picked.length > 1) {
      const ok = await this.confirm.confirm({
        title: `Approve ${picked.length} orders?`,
        message:
          'Each order goes to your broker in the window before the next open. ' +
          'You can still stop trading from any page until then.',
        confirmLabel: `Approve ${picked.length}`,
        ticket: {
          kind: 'Approve the selected orders',
          live: this.pickedLive(),
          lines: [
            { label: 'Orders', value: String(picked.length) },
            { label: 'About', value: this.pickedNotional() },
          ],
        },
      });
      if (!ok) return;
    }
    this.bulkBusy.set(true);
    try {
      for (const g of groups) await this.approve(g.group, g.tickets);
      this.picked.set(this.emptySet);
    } finally {
      this.bulkBusy.set(false);
    }
  }

  /** Reject the ticked tickets with one reason. */
  protected async rejectPicked(): Promise<void> {
    const picked = this.pickedTickets();
    if (!picked.length) return;
    const what = picked.length === 1 ? this.orderWords(picked[0]) : `${picked.length} orders`;
    const reason = await this.rejectSheet().open(what, this.pickedLive());
    if (reason === null) return;
    this.bulkBusy.set(true);
    const ids = picked.map((t) => t.id);
    this.mark(ids, true);
    try {
      const done = [];
      for (const t of picked) done.push(await this.api.reject(t.id, reason));
      this.replace(done);
      this.picked.set(this.emptySet);
      this.toasts.success(`${what} rejected. Nothing goes to your broker for them.`);
    } catch {
      this.list.reload();
    } finally {
      this.mark(ids, false);
      this.bulkBusy.set(false);
    }
  }

  private orderWords(t: TicketView): string {
    return `${t.side === 'buy' ? 'Buy' : 'Sell'} ${t.ticker}`;
  }

  protected readonly columns: readonly TableColumn<TicketView>[] = [
    { key: 'ticker', label: 'Order', mobile: 'title', sortable: false },
    { key: 'portfolio_name', label: 'Portfolio' },
    { key: 'quantity', label: 'Quantity', format: 'number' },
    {
      key: 'notional',
      label: 'About',
      format: 'money',
      currency: (t) => this.currencyOf(t.portfolio_id),
    },
    { key: 'status', label: 'Status', value: (t) => ticketStatusLook(t.status).label },
    { key: 'decided_by', label: 'Decided by', value: (t) => deciderWords(t.decided_by) },
    { key: 'as_of', label: 'Decided', format: 'date' },
  ];
  protected readonly key = (t: TicketView) => t.id;
  protected readonly look = ticketStatusLook;
  protected readonly hold = (t: TicketView) => holdWords(t.hold);
  protected readonly rules = ruleLines;
  protected readonly strategyName = (t: TicketView) => rowStrategyName(t);
  /** Money in the ticket's portfolio currency (USD while the list is unknown). */
  protected readonly money = (v: number | null | undefined, t?: { portfolio_id: string }) =>
    formatMoney(v, { currency: t ? this.currencyOf(t.portfolio_id) : undefined });

  private currencyOf(portfolioId: string): string | undefined {
    return this.portfolios.options().find((p) => p.id === portfolioId)?.base_currency;
  }
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
    // Busy from the first click: a second one while the code check runs sends nothing.
    this.mark(ids, true);
    try {
      if (!(await this.stepUp.ensure(`Approve ${what}.`))) return;
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
          {
            label: 'About',
            value: this.money(group.notional, { portfolio_id: group.portfolioId }),
          },
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
    const reason = await this.rejectSheet().open(what, this.isLive(ticket.portfolio_id));
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
