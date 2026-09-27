import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  resource,
  signal,
} from '@angular/core';

import { CashFlowsService } from '../../api/cash-flows.service';
import { InsightsService } from '../../api/insights.service';
import type { CashFlowView } from '../../api/models';
import { SessionService } from '../../core/auth/session.service';
import { ConfirmService } from '../../core/confirm/confirm.service';
import { formatDate, formatMoney, formatPercent, isoDay } from '../../core/format/format';
import { ToastService } from '../../core/notify/toast.service';
import { PortfolioContextService } from '../../core/portfolio/portfolio-context.service';
import { DataTable, type TableColumn } from '../../shared/ui/data-table/data-table';
import { NoBook, bookState } from '../../shared/ui/no-book';
import { PageHeader } from '../../shared/ui/page-header';
import { PermissionNote } from '../../shared/ui/permission-note';
import { Segmented, type SegmentOption } from '../../shared/ui/segmented';
import { StatTile } from '../../shared/ui/stat-tile';
import { EmptyState, ErrorState, LoadingState } from '../../shared/ui/states';
import { InsightsNav } from './insights-nav';

type FlowKind = CashFlowView['kind'];

export const FLOW_KINDS: readonly SegmentOption<FlowKind>[] = [
  { value: 'deposit', label: 'Deposit' },
  { value: 'withdrawal', label: 'Withdrawal' },
];

/** A flow as the change it made to the cash: withdrawals negative. */
export function signedAmount(f: Pick<CashFlowView, 'kind' | 'amount'>): number {
  return f.kind === 'withdrawal' ? -f.amount : f.amount;
}

/**
 * Deposits and withdrawals of the picked portfolio, and a form to record
 * one. Returns take them out (TWR and MWR), so a deposit never shows as
 * profit. Recording one moves a simulated book's cash, so it asks first, as
 * a ticket. A broker book gets its flows from the sync.
 */
@Component({
  selector: 'app-cash-flows-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    PageHeader,
    InsightsNav,
    NoBook,
    StatTile,
    DataTable,
    Segmented,
    PermissionNote,
    LoadingState,
    EmptyState,
    ErrorState,
  ],
  template: `
    <app-page-header
      title="Cash flows"
      description="Money you put in or took out. Returns leave it out, so a deposit is never profit."
    />
    <app-insights-nav />

    @if (book() === 'none') {
      <app-no-book message="Deposits and withdrawals show here once you have a portfolio." />
    } @else {
      <section class="tiles" aria-label="Returns without deposits and withdrawals">
        <app-stat-tile
          label="Time-weighted return"
          help="twr"
          [loading]="insights.isLoading() && !insights.hasValue()"
          [value]="twr()"
          detail="Since the start"
        />
        <app-stat-tile
          label="Money-weighted return"
          help="mwr"
          [loading]="insights.isLoading() && !insights.hasValue()"
          [value]="mwr()"
          detail="Per year, since the start"
        />
        <app-stat-tile
          label="Net deposits"
          [help]="false"
          [loading]="insights.isLoading() && !insights.hasValue()"
          [value]="netFlows()"
          detail="Deposits less withdrawals"
        />
      </section>

      <div class="page-grid">
        <section class="panel span-5" aria-labelledby="record-title">
          <div class="panel-head">
            <h2 id="record-title">Record a deposit or withdrawal</h2>
          </div>
          <div class="panel-body">
            @if (isBroker()) {
              <p class="muted">
                This portfolio is at a broker, so its deposits and withdrawals come in with each
                broker sync.
              </p>
            } @else {
              <form class="form-grid" (submit)="$event.preventDefault(); record()" novalidate>
                <app-segmented label="Kind" [options]="kinds" [(value)]="kind" />
                <div class="field">
                  <label for="flow-amount">Amount ({{ currency() }})</label>
                  <input
                    id="flow-amount"
                    class="input num"
                    type="number"
                    inputmode="decimal"
                    min="0.01"
                    step="0.01"
                    [disabled]="!canManage()"
                    [attr.aria-invalid]="amountError() ? true : null"
                    [attr.aria-describedby]="amountError() ? 'flow-amount-error' : null"
                    [value]="amount()"
                    (input)="amount.set($any($event.target).value)"
                  />
                  @if (amountError(); as e) {
                    <span id="flow-amount-error" class="error">{{ e }}</span>
                  }
                </div>
                <div class="field">
                  <label for="flow-date">Date</label>
                  <input
                    id="flow-date"
                    class="input num"
                    type="date"
                    [max]="today"
                    [disabled]="!canManage()"
                    aria-describedby="flow-date-hint"
                    [value]="day()"
                    (input)="day.set($any($event.target).value)"
                  />
                  <span id="flow-date-hint" class="hint"
                    >Not before the last trading run, and not in the future.</span
                  >
                </div>
                <div class="field">
                  <label for="flow-note">Note (optional)</label>
                  <input
                    id="flow-note"
                    class="input"
                    maxlength="200"
                    autocomplete="off"
                    [disabled]="!canManage()"
                    [value]="note()"
                    (input)="note.set($any($event.target).value)"
                  />
                </div>
                <div class="actions">
                  <button
                    type="submit"
                    class="btn btn-primary"
                    [disabled]="!canManage() || busy()"
                    [attr.aria-busy]="busy()"
                  >
                    {{ kind() === 'deposit' ? 'Record deposit' : 'Record withdrawal' }}
                  </button>
                  <app-permission-note permission="portfolio.manage" />
                </div>
              </form>
            }
          </div>
        </section>

        <section class="panel span-7" aria-labelledby="flows-title">
          <div class="panel-head">
            <h2 id="flows-title">Deposits and withdrawals</h2>
          </div>
          <div class="panel-body">
            @if (flows.error(); as err) {
              <app-error-state
                title="Could not load the cash flows"
                [error]="err"
                (retry)="flows.reload()"
              />
            } @else if (!flows.hasValue()) {
              <app-loading-state label="Loading cash flows" [rows]="4" />
            } @else if (flows.value().length === 0) {
              <app-empty-state
                title="No deposits or withdrawals yet"
                message="Record one here when you add or take out money, so your returns stay right."
              />
            } @else {
              <app-data-table
                caption="Deposits and withdrawals, newest first"
                [rows]="newestFirst()"
                [columns]="columns"
                [rowKey]="flowKey"
              />
            }
          </div>
        </section>
      </div>
    }
  `,
  styles: `
    @use 'breakpoints' as bp;
    .tiles {
      display: grid;
      gap: var(--space-3);
      grid-template-columns: minmax(0, 1fr);
      margin-bottom: var(--space-4);
      @include bp.from-tablet {
        grid-template-columns: repeat(3, minmax(0, 1fr));
      }
    }
    .actions {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: var(--space-2);
    }
  `,
})
export class CashFlowsPage {
  private readonly api = inject(CashFlowsService);
  private readonly insightsApi = inject(InsightsService);
  private readonly ctx = inject(PortfolioContextService);
  private readonly session = inject(SessionService);
  private readonly confirm = inject(ConfirmService);
  private readonly toasts = inject(ToastService);

  protected readonly book = computed(() => bookState(this.ctx));
  private readonly portfolio = computed(() =>
    this.book() === 'ready' ? this.ctx.current() : null,
  );
  protected readonly isBroker = computed(() => this.portfolio()?.kind === 'broker');
  protected readonly currency = computed(() => this.portfolio()?.base_currency ?? 'USD');
  protected readonly canManage = computed(() => this.session.can('portfolio.manage'));

  protected readonly flows = resource({
    params: () => {
      const p = this.portfolio();
      return p ? { id: p.id } : undefined;
    },
    loader: ({ params }) => this.api.list(params.id),
  });
  protected readonly insights = resource({
    params: () => (this.portfolio() ? { portfolio: this.ctx.selectedId() } : undefined),
    loader: () => this.insightsApi.get(),
  });

  protected readonly newestFirst = computed(() =>
    this.flows.hasValue() ? [...this.flows.value()].reverse() : [],
  );

  protected readonly twr = computed(() => {
    if (!this.insights.hasValue()) return '–';
    const start = this.insights.value().pnl.find((p) => p.period === 'inception');
    return start?.twr == null ? 'n/a' : formatPercent(start.twr, { digits: 1, signed: true });
  });
  protected readonly mwr = computed(() => {
    if (!this.insights.hasValue()) return '–';
    const m = this.insights.value().mwr;
    return m == null ? 'n/a' : formatPercent(m, { digits: 1, signed: true });
  });
  protected readonly netFlows = computed(() =>
    this.insights.hasValue()
      ? formatMoney(this.insights.value().net_flows ?? 0, {
          signed: true,
          currency: this.insights.value().currency,
        })
      : '–',
  );

  protected readonly kinds = FLOW_KINDS;
  protected readonly kind = signal<FlowKind>('deposit');
  protected readonly amount = signal('');
  protected readonly day = signal('');
  protected readonly note = signal('');
  protected readonly amountError = signal<string | null>(null);
  protected readonly busy = signal(false);
  protected readonly today = isoDay();

  protected readonly columns: TableColumn<CashFlowView>[] = [
    {
      key: 'flow_date',
      label: 'Date',
      format: 'date',
      mobile: 'title',
    },
    {
      key: 'kind',
      label: 'Kind',
      value: (f) => (f.kind === 'deposit' ? 'Deposit' : 'Withdrawal'),
    },
    {
      key: 'amount',
      label: 'Amount',
      format: 'signedMoney',
      tone: true,
      value: signedAmount,
      currency: () => this.currency(),
    },
    {
      key: 'source',
      label: 'From',
      value: (f) => (f.source === 'broker' ? 'Broker sync' : 'Recorded here'),
    },
    { key: 'note', label: 'Note', value: (f) => f.note ?? '', mobile: 'hide', sortable: false },
  ];
  protected readonly flowKey = (f: CashFlowView) =>
    f.id != null ? `r${f.id}` : `b${f.flow_date}${f.kind}${f.amount}`;

  protected async record(): Promise<void> {
    const p = this.portfolio();
    if (!p || this.busy()) return;
    const amount = Number(String(this.amount()).trim());
    if (!String(this.amount()).trim() || !Number.isFinite(amount) || amount <= 0) {
      this.amountError.set('Enter an amount above 0.');
      return;
    }
    this.amountError.set(null);
    const kind = this.kind();
    const words = kind === 'deposit' ? 'deposit' : 'withdrawal';
    const money = formatMoney(amount, { currency: this.currency() });
    const day = this.day() || this.today;
    const ok = await this.confirm.confirm({
      title: `Record a ${words} of ${money}?`,
      message:
        kind === 'deposit'
          ? 'The cash of this paper portfolio goes up by this amount.'
          : 'The cash of this paper portfolio goes down by this amount.',
      confirmLabel: kind === 'deposit' ? 'Record deposit' : 'Record withdrawal',
      ticket: {
        kind: kind === 'deposit' ? 'Deposit' : 'Withdrawal',
        live: this.ctx.live(),
        lines: [
          { label: 'Portfolio', value: p.name },
          { label: 'Amount', value: money },
          { label: 'Date', value: formatDate(day) },
        ],
      },
    });
    if (!ok) return;
    this.busy.set(true);
    try {
      await this.api.record(p.id, {
        kind,
        amount,
        flow_date: this.day() || null,
        note: this.note().trim() || null,
      });
      this.toasts.success(`Recorded the ${words} of ${money}.`);
      this.amount.set('');
      this.note.set('');
      this.day.set('');
      this.flows.reload();
      this.insights.reload();
    } catch {
      // The error interceptor already showed the API's message.
    } finally {
      this.busy.set(false);
    }
  }
}
