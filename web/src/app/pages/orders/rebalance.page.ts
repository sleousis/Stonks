import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  linkedSignal,
  resource,
  signal,
} from '@angular/core';
import { FormsModule } from '@angular/forms';
import { RouterLink } from '@angular/router';

import { ExecutionService } from '../../api/execution.service';
import type {
  AlgoSettingView,
  PlanLineView,
  PlanRequest,
  RebalancePlanView,
} from '../../api/generated/types.gen';
import { SessionService } from '../../core/auth/session.service';
import { ConfirmService } from '../../core/confirm/confirm.service';
import { formatMoney, formatNumber, formatPercent } from '../../core/format/format';
import { errorMessage } from '../../core/http/api-error';
import { ToastService } from '../../core/notify/toast.service';
import { PortfolioContextService } from '../../core/portfolio/portfolio-context.service';
import { type TableColumn, DataTable } from '../../shared/ui/data-table/data-table';
import { PermissionNote } from '../../shared/ui/permission-note';
import { type SegmentOption, Segmented } from '../../shared/ui/segmented';
import { EmptyState, ErrorState, LoadingState } from '../../shared/ui/states';

type Source = 'targets' | 'strategy';
type AlgoChoice = 'plain' | 'adaptive' | 'twap' | 'vwap';

const SOURCES: SegmentOption<Source>[] = [
  { value: 'targets', label: 'My targets' },
  { value: 'strategy', label: 'A strategy' },
];

/** Parses `AAPL.US=0.3` lines (commas or new lines). Weights may be percents. */
export function parseTargets(text: string): { ticker: string; weight: number }[] | string {
  const out: { ticker: string; weight: number }[] = [];
  for (const raw of text.split(/[\n,]/)) {
    const part = raw.trim();
    if (!part) continue;
    const [ticker, weightText] = part.split('=').map((s) => s.trim());
    const pct = weightText?.endsWith('%');
    const weight = Number(pct ? weightText.slice(0, -1) : weightText);
    if (!ticker || !weightText || !Number.isFinite(weight) || weight < 0) {
      return `"${part}" is not TICKER=WEIGHT`;
    }
    out.push({ ticker: ticker.toUpperCase(), weight: pct ? weight / 100 : weight });
  }
  return out;
}

/** Plain words for why a line has no trade. */
export function skipText(reason: string | null | undefined): string {
  switch (reason) {
    case 'no_price':
      return 'No price';
    case 'below_min_trade':
      return 'Below the smallest trade';
    case 'below_one_share':
      return 'Less than one share';
    case 'no_cash':
      return 'Not enough cash';
    default:
      return '';
  }
}

/** A short line naming how an algo setting works the orders. */
export function algoText(s: Pick<AlgoSettingView, 'algo' | 'params'> | null | undefined): string {
  if (!s) return 'Plain limit orders';
  const p = s.params as Record<string, unknown>;
  if (s.algo === 'adaptive') return `Adaptive, ${String(p['priority'] ?? 'normal')}`;
  const start = Number(p['start_minutes'] ?? 0);
  const end = p['end_minutes'];
  const window =
    end == null
      ? `from ${start} min after the open to the close`
      : `${start} to ${String(end)} min after the open`;
  return `${s.algo.toUpperCase()}, ${window}`;
}

/**
 * The rebalancing planner: the trades that move a portfolio to target
 * weights, with costs, turnover and a tax preview, before anything is
 * sent. Writing the plan makes order tickets that wait on Approvals for a
 * fresh code. The second panel sets how the portfolio's orders are worked
 * (plain, Adaptive, TWAP or VWAP) and lists the parent orders Stonks slices.
 */
@Component({
  selector: 'app-rebalance-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    FormsModule,
    RouterLink,
    Segmented,
    DataTable,
    PermissionNote,
    LoadingState,
    EmptyState,
    ErrorState,
  ],
  template: `
    <section class="panel" aria-labelledby="plan-title">
      <div class="panel-head">
        <h2 id="plan-title">Rebalance</h2>
        <app-segmented label="Plan towards" [options]="sources" [(value)]="source" />
      </div>
      <form class="body form-grid form-grid-2" (submit)="$event.preventDefault(); preview()">
        @if (source() === 'targets') {
          <div class="field wide">
            <label for="rb-targets">Target weights</label>
            <textarea
              id="rb-targets"
              class="input"
              rows="4"
              name="targets"
              placeholder="AAPL.US=30%&#10;MSFT.US=0.2"
              [(ngModel)]="targetsText"
            ></textarea>
            <small class="muted">One per line. The rest stays in cash.</small>
          </div>
        } @else {
          <div class="field wide">
            <label for="rb-strategy">Strategy id</label>
            <input
              id="rb-strategy"
              class="input"
              name="strategy"
              autocomplete="off"
              [(ngModel)]="strategyId"
            />
            <small class="muted">The latest weights of its test book are the targets.</small>
          </div>
        }
        <div class="field">
          <label for="rb-min">Smallest trade</label>
          <input
            id="rb-min"
            class="input num"
            type="number"
            min="0"
            name="min"
            [(ngModel)]="minTrade"
          />
        </div>
        <div class="field">
          <label for="rb-short">Tax on short-term gains (%)</label>
          <input
            id="rb-short"
            class="input num"
            type="number"
            min="0"
            max="100"
            name="short"
            [(ngModel)]="shortRate"
          />
        </div>
        <div class="field">
          <label for="rb-long">Tax on long-term gains (%)</label>
          <input
            id="rb-long"
            class="input num"
            type="number"
            min="0"
            max="100"
            name="long"
            [(ngModel)]="longRate"
          />
        </div>
        <div class="actions wide">
          <button type="submit" class="btn btn-primary" [disabled]="busy() || !portfolioId()">
            Preview trades
          </button>
        </div>
      </form>
      @if (problem(); as p) {
        <p class="failure" role="alert">{{ p }}</p>
      }
      @if (plan(); as pl) {
        <div class="summary" aria-live="polite">
          <p>
            <span class="muted">Value</span> <b class="num">{{ money(pl.equity) }}</b>
          </p>
          <p>
            <span class="muted">Turnover</span> <b class="num">{{ pct(pl.turnover) }}</b>
          </p>
          <p>
            <span class="muted">Costs</span> <b class="num">{{ money(pl.total_cost) }}</b>
          </p>
          <p>
            <span class="muted">Cash after</span> <b class="num">{{ money(pl.cash_after) }}</b>
          </p>
          @if (pl.tax_total !== null) {
            <p>
              <span class="muted">Estimated tax</span> <b class="num">{{ money(pl.tax_total) }}</b>
            </p>
          }
        </div>
        @if (pl.algo) {
          <p class="note">Worked with {{ algoLine(pl.algo) }}.</p>
        }
        @for (n of pl.notes; track n) {
          <p class="note">{{ n }}</p>
        }
        <app-data-table
          caption="Planned trades"
          [rows]="pl.lines"
          [columns]="columns"
          [rowKey]="lineKey"
          emptyMessage="Nothing to trade."
        />
        <div class="actions body">
          <input
            class="input reason"
            aria-label="Why you rebalance"
            placeholder="Why you rebalance"
            name="reason"
            [(ngModel)]="reason"
          />
          <button
            type="button"
            class="btn btn-primary"
            [disabled]="busy() || trades().length === 0 || !canTrade() || reason.trim().length < 3"
            (click)="write()"
          >
            Write {{ trades().length }} ticket(s)
          </button>
        </div>
        <p class="note">
          Tickets wait on <a routerLink="/tickets">Approvals</a> for your code before they are sent.
        </p>
        <div class="note-row"><app-permission-note permission="portfolio.trade" /></div>
      }
    </section>

    <section class="panel" aria-labelledby="algo-title">
      <div class="panel-head"><h2 id="algo-title">How orders are worked</h2></div>
      @if (settings.error(); as err) {
        <app-error-state
          title="Could not load the setting"
          [error]="err"
          (retry)="settings.reload()"
        />
      } @else if (!settings.hasValue()) {
        <app-loading-state label="Loading the setting" [rows]="1" />
      } @else {
        <p class="body">
          Now: <b>{{ algoLine(ownSetting()) }}</b>
        </p>
        <form class="body form-grid form-grid-2" (submit)="$event.preventDefault(); saveAlgo()">
          <div class="field">
            <label for="rb-algo">Algo</label>
            <select id="rb-algo" class="input" name="algo" [(ngModel)]="algo">
              <option value="plain">Plain limit orders</option>
              <option value="adaptive">Adaptive (IBKR)</option>
              <option value="twap">TWAP</option>
              <option value="vwap">VWAP</option>
            </select>
          </div>
          @if (algo() === 'adaptive') {
            <div class="field">
              <label for="rb-priority">Priority</label>
              <select id="rb-priority" class="input" name="priority" [(ngModel)]="priority">
                <option value="patient">Patient</option>
                <option value="normal">Normal</option>
                <option value="urgent">Urgent</option>
              </select>
            </div>
          } @else if (algo() !== 'plain') {
            <div class="field">
              <label for="rb-end">Minutes after the open to finish</label>
              <input
                id="rb-end"
                class="input num"
                type="number"
                min="1"
                name="end"
                [(ngModel)]="endMinutes"
              />
            </div>
          }
          <div class="actions wide">
            <button type="submit" class="btn btn-primary" [disabled]="busy() || !canManage()">
              Save
            </button>
          </div>
        </form>
        <p class="note">
          IBKR runs these algos itself. At other brokers Stonks sends TWAP and VWAP as small orders
          through the window, and Adaptive goes out plain.
        </p>
        <div class="note-row"><app-permission-note permission="portfolio.manage" /></div>
      }
      @if (parents.hasValue() && parents.value().items.length > 0) {
        <h3 class="body">Orders Stonks is slicing</h3>
        <ul class="parents body">
          @for (p of parents.value().items; track p.client_id) {
            <li>
              {{ p.side }} {{ num(p.quantity) }} {{ p.ticker }} by {{ p.algo.toUpperCase() }}:
              {{ num(p.filled) }} filled, {{ p.state }}
            </li>
          }
        </ul>
      } @else if (parents.hasValue()) {
        <app-empty-state title="No sliced orders" message="Parent orders Stonks works show here." />
      }
    </section>
  `,
  styles: `
    @use 'breakpoints' as bp;
    :host {
      display: grid;
      gap: var(--space-4);
      min-width: 0;
    }
    .body {
      padding: var(--space-3) var(--space-4);
    }
    .wide {
      grid-column: 1 / -1;
    }
    .actions {
      display: flex;
      flex-wrap: wrap;
      gap: var(--space-2);
      justify-content: flex-end;
      align-items: center;
    }
    .actions .btn {
      min-height: var(--touch-min);
    }
    .reason {
      flex: 1 1 240px;
      min-height: var(--touch-min);
    }
    .summary {
      display: flex;
      flex-wrap: wrap;
      gap: var(--space-4);
      padding: var(--space-3) var(--space-4);
    }
    .note,
    .failure {
      padding: 0 var(--space-4) var(--space-2);
      font-size: var(--text-sm);
      color: var(--color-ink-2);
      overflow-wrap: anywhere;
    }
    .failure {
      color: var(--color-loss);
    }
    .note-row {
      padding: 0 var(--space-4) var(--space-3);
    }
    .parents {
      margin: 0;
      padding-left: var(--space-6);
    }
    @include bp.phone {
      .summary {
        gap: var(--space-2);
      }
    }
  `,
})
export class RebalancePage {
  private readonly api = inject(ExecutionService);
  private readonly ctx = inject(PortfolioContextService);
  private readonly session = inject(SessionService);
  private readonly confirmer = inject(ConfirmService);
  private readonly toasts = inject(ToastService);

  protected readonly sources = SOURCES;
  protected readonly source = signal<Source>('targets');
  protected targetsText = '';
  protected strategyId = '';
  protected minTrade = 0;
  protected shortRate: number | null = null;
  protected longRate: number | null = null;
  protected reason = '';

  protected readonly busy = signal(false);
  protected readonly problem = signal<string | null>(null);
  protected readonly plan = signal<RebalancePlanView | null>(null);
  /** The request the shown plan was made from; Write tickets sends only this. */
  private planned: PlanRequest | null = null;
  protected readonly portfolioId = computed(() => this.ctx.current()?.id ?? null);
  protected readonly canTrade = computed(() => this.session.can('portfolio.trade'));
  protected readonly canManage = computed(() => this.session.can('portfolio.manage'));
  protected readonly trades = computed(() => (this.plan()?.lines ?? []).filter((l) => l.side));

  protected readonly settings = resource({
    params: () => this.portfolioId() ?? undefined,
    loader: ({ params }) => this.api.settings(params),
  });
  protected readonly parents = resource({
    params: () => this.portfolioId() ?? undefined,
    loader: ({ params }) => this.api.parents(params),
  });
  protected readonly ownSetting = computed(
    () => this.settings.value()?.items.find((s) => !s.strategy_id) ?? null,
  );
  /** The form starts from the stored setting, so Save on an untouched form keeps it. */
  protected readonly algo = linkedSignal<AlgoChoice>(
    () => (this.ownSetting()?.algo as AlgoChoice | undefined) ?? 'plain',
  );
  protected readonly priority = linkedSignal(() =>
    String(this.ownSetting()?.params?.['priority'] ?? 'normal'),
  );
  protected readonly endMinutes = linkedSignal<number | null>(() => {
    const end = this.ownSetting()?.params?.['end_minutes'];
    return typeof end === 'number' ? end : null;
  });

  private readonly currency = computed(() => this.ctx.current()?.base_currency ?? 'USD');
  protected readonly money = (v: number | null | undefined) =>
    formatMoney(v, { currency: this.currency() });
  protected readonly pct = (v: number | null | undefined) => formatPercent(v);
  protected readonly num = (v: number) => formatNumber(v);
  protected readonly algoLine = (s: { name?: string; algo?: string; params?: unknown } | null) =>
    s
      ? algoText({ algo: (s.algo ?? s.name) as string, params: (s.params ?? {}) as never })
      : algoText(null);
  protected readonly lineKey = (l: PlanLineView) => l.ticker;

  protected readonly columns: TableColumn<PlanLineView>[] = [
    { key: 'ticker', label: 'Ticker', mobile: 'title', help: false },
    { key: 'current_weight', label: 'Now', format: 'percent', help: false },
    { key: 'target_weight', label: 'Target', format: 'percent', help: false },
    {
      key: 'trade',
      label: 'Trade',
      value: (l) => (l.side ? `${l.side} ${formatNumber(l.quantity)}` : skipText(l.skipped)),
      help: false,
    },
    { key: 'value', label: 'Value', format: 'money', currency: () => this.currency(), help: false },
    { key: 'cost', label: 'Cost', format: 'money', currency: () => this.currency(), help: false },
    { key: 'weight_after', label: 'After', format: 'percent', help: false },
    {
      key: 'tax',
      label: 'Gain',
      value: (l) => l.tax?.gain ?? null,
      format: 'signedMoney',
      currency: () => this.currency(),
      help: false,
    },
  ];

  private request(): PlanRequest | string {
    const portfolio = this.portfolioId();
    if (!portfolio) return 'Pick a portfolio first.';
    const base = {
      portfolio_id: portfolio,
      min_trade_value: Number(this.minTrade) || 0,
      short_term_rate: this.shortRate == null ? null : Number(this.shortRate) / 100,
      long_term_rate: this.longRate == null ? null : Number(this.longRate) / 100,
    };
    if (this.source() === 'strategy') {
      if (!this.strategyId.trim()) return 'Name the strategy.';
      return { ...base, source: 'strategy', strategy_id: this.strategyId.trim() };
    }
    const targets = parseTargets(this.targetsText);
    if (typeof targets === 'string') return targets;
    return { ...base, source: 'targets', targets };
  }

  protected async preview(): Promise<void> {
    const body = this.request();
    if (typeof body === 'string') {
      this.problem.set(body);
      return;
    }
    this.busy.set(true);
    this.problem.set(null);
    try {
      this.plan.set(await this.api.plan(body));
      this.planned = body;
    } catch (err) {
      this.plan.set(null);
      this.planned = null;
      this.problem.set(errorMessage(err));
    } finally {
      this.busy.set(false);
    }
  }

  protected async write(): Promise<void> {
    const body = this.request();
    const pl = this.plan();
    if (typeof body === 'string' || !pl) return;
    // The form or the portfolio changed since the preview: the plan on screen
    // no longer says what would be written.
    if (JSON.stringify(body) !== JSON.stringify(this.planned)) {
      this.plan.set(null);
      this.planned = null;
      this.problem.set('The form changed since the preview. Preview the plan again.');
      return;
    }
    const ok = await this.confirmer.confirm({
      title: `Write ${this.trades().length} order ticket(s)?`,
      message: 'Each ticket waits on Approvals for your code. Nothing is sent before that.',
      confirmLabel: 'Write tickets',
      ticket: {
        live: this.ctx.live(),
        lines: [
          { label: 'Turnover', value: formatPercent(pl.turnover) },
          { label: 'Costs', value: this.money(pl.total_cost) },
        ],
      },
    });
    if (!ok) return;
    this.busy.set(true);
    try {
      const done = await this.api.confirm({ ...body, reason: this.reason.trim() });
      this.toasts.success(
        done.written
          ? `Wrote ${done.written} ticket(s). Approve them on Approvals.`
          : 'These tickets were written before.',
      );
    } catch (err) {
      this.problem.set(errorMessage(err));
    } finally {
      this.busy.set(false);
    }
  }

  protected async saveAlgo(): Promise<void> {
    const portfolio = this.portfolioId();
    if (!portfolio) return;
    this.busy.set(true);
    try {
      const algo = this.algo();
      const end = this.endMinutes();
      if (algo === 'plain') {
        await this.api.clearAlgo(portfolio);
      } else {
        const params: Record<string, unknown> =
          algo === 'adaptive'
            ? { priority: this.priority() }
            : end
              ? { end_minutes: Number(end) }
              : {};
        await this.api.setAlgo(portfolio, { algo, params });
      }
      this.toasts.success('Saved how orders are worked.');
      this.settings.reload();
    } catch (err) {
      this.problem.set(errorMessage(err));
    } finally {
      this.busy.set(false);
    }
  }
}
