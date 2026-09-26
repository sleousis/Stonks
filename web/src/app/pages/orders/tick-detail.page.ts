import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  input,
  resource,
} from '@angular/core';
import { RouterLink } from '@angular/router';

import type { FillView, RiskAdjustmentView, ShadowOutcomeView } from '../../api/models';
import { OrdersService } from '../../api/orders.service';
import { TicksService } from '../../api/ticks.service';
import { formatDateTime, formatDuration, formatPercent } from '../../core/format/format';
import { DataTable, TableCell, type TableColumn } from '../../shared/ui/data-table/data-table';
import { StatTile } from '../../shared/ui/stat-tile';
import { EmptyState, ErrorState, LoadingState } from '../../shared/ui/states';
import { SideTag } from '../../shared/ui/side-tag';
import { StatusPill } from '../../shared/ui/status-pill';
import { FILL_COLUMNS } from './fills.page';
import { OrdersTable } from './orders-table';
import { humanize, tickNotes, tickOutcome } from './tick-summary';

/** Fills shown on a run's page; the fills page has the rest. */
export const TICK_FILLS_LIMIT = 200;

/**
 * One trading run (tick): what it decided (winner or exit), what the risk
 * policy changed, the orders and fills it produced, and how each shadow
 * strategy fared.
 */
@Component({
  selector: 'app-tick-detail-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    RouterLink,
    DataTable,
    TableCell,
    StatTile,
    StatusPill,
    SideTag,
    OrdersTable,
    LoadingState,
    EmptyState,
    ErrorState,
  ],
  template: `
    <a class="back-link" routerLink="/orders/ticks"
      ><span aria-hidden="true">←</span> All trading runs</a
    >

    @if (tick.error(); as err) {
      <app-error-state
        title="Could not load this trading run"
        [error]="err"
        (retry)="tick.reload()"
      />
    } @else if (!tick.hasValue()) {
      <app-loading-state label="Loading the trading run" [rows]="6" />
    } @else {
      @let t = tick.value();
      @let s = t.summary;
      <div class="detail-head">
        <h2>Trading run of {{ dateTime(t.started_at) }}</h2>
        <app-status-pill [status]="t.status" />
        <span class="run-id muted"
          >Run id <span class="mono">{{ t.id }}</span></span
        >
      </div>

      <section class="tiles" aria-label="Trading run summary">
        <app-stat-tile
          label="Started"
          [value]="dateTime(t.started_at)"
          [detail]="'Took ' + took()"
        />
        <app-stat-tile label="Orders placed" [value]="count(s?.orders_placed)" />
        <app-stat-tile label="Fills" [value]="count(s?.fills)" />
        <app-stat-tile
          [label]="outcome().kind === 'exit' ? 'Exit strategy' : 'Winner'"
          [value]="outcome().strategyId ?? outcome().text"
          [detail]="outcomeDetail()"
          featured
        />
        <app-stat-tile
          label="Risk adjustments"
          [value]="count(risks().length)"
          [detail]="shadowCount()"
        />
      </section>

      <div class="page-grid">
        <section class="panel span-12" aria-labelledby="decision-title">
          <div class="panel-head">
            <h2 id="decision-title">Decision</h2>
          </div>
          @if (s?.error) {
            <p class="alert-line" role="alert">
              <strong>{{ s?.error_type ?? 'Error' }}:</strong> {{ s?.error }}
            </p>
          }
          @for (note of notes(); track note) {
            <p class="alert-line warn">{{ note }}</p>
          }
          <dl class="facts">
            <div>
              <dt>{{ outcome().kind === 'exit' ? 'Exited positions of' : 'Winning strategy' }}</dt>
              <dd>
                @if (outcome().strategyId; as id) {
                  <a class="cell-link" routerLink="/strategies/{{ id }}">{{ id }}</a>
                } @else {
                  {{ outcome().text }}
                }
              </dd>
            </div>
            <div>
              <dt>Expected return</dt>
              <dd class="num">{{ expectedReturn() }}</dd>
            </div>
            <div>
              <dt>Reason</dt>
              <dd>{{ s?.reason ? humanize(s.reason) : '–' }}</dd>
            </div>
            <div>
              <dt>Finished</dt>
              <dd class="num">{{ dateTime(t.finished_at) }}</dd>
            </div>
          </dl>
        </section>

        <section class="panel span-12" aria-labelledby="risk-title">
          <div class="panel-head">
            <h2 id="risk-title">Risk adjustments</h2>
          </div>
          @if (risks().length === 0) {
            <app-empty-state
              title="No adjustments"
              message="The risk policy let every order through as the strategy asked."
            />
          } @else {
            <app-data-table
              caption="Orders the risk policy clipped or blocked"
              [rows]="risks()"
              [columns]="riskColumns"
              [rowKey]="riskKey"
              [pageSize]="0"
            >
              <ng-template appCell="side" [appCellOf]="risks()" let-r>
                <app-side-tag [side]="r.side" />
              </ng-template>
            </app-data-table>
          }
        </section>

        <section class="panel span-12" aria-labelledby="tick-orders-title">
          <div class="panel-head">
            <h2 id="tick-orders-title">Orders</h2>
            <span class="count num">{{ t.orders.length }}</span>
          </div>
          @if (t.orders.length === 0) {
            <app-empty-state
              title="No orders"
              message="Dry runs and runs without a winner place no orders."
            />
          } @else {
            <app-orders-table
              caption="Orders this trading run placed"
              [rows]="t.orders"
              [pageSize]="25"
              [linkTicks]="false"
            />
          }
        </section>

        <section class="panel span-6" aria-labelledby="tick-fills-title">
          <div class="panel-head">
            <h2 id="tick-fills-title">Fills</h2>
            @if (fillsCapped(); as cap) {
              <a class="cell-link" routerLink="/orders/fills" [queryParams]="{ tick: t.id }"
                ><span class="num">{{ cap.shown }} of {{ cap.total }}</span
                >, see all fills</a
              >
            }
          </div>
          @if (fills.error(); as err) {
            <app-error-state title="Could not load fills" [error]="err" (retry)="fills.reload()" />
          } @else if (!fills.hasValue()) {
            <app-loading-state label="Loading fills" [rows]="3" />
          } @else if (fills.value().items.length === 0) {
            <app-empty-state title="No fills" message="Nothing was filled on this run." />
          } @else {
            <app-data-table
              caption="Fills for this trading run"
              [rows]="fills.value().items"
              [columns]="fillColumns"
              [rowKey]="fillKey"
              [pageSize]="25"
            >
              <ng-template appCell="order_client_id" [appCellOf]="fills.value().items" let-f>
                <a class="cell-link" [routerLink]="['/trades/orders', f.order_client_id]"
                  >View order</a
                >
              </ng-template>
            </app-data-table>
          }
        </section>

        <section class="panel span-6" aria-labelledby="shadow-title">
          <div class="panel-head">
            <h2 id="shadow-title">Shadow outcomes</h2>
            <a class="cell-link" routerLink="/shadow">Open shadow</a>
          </div>
          @if (s?.shadow_error) {
            <p class="alert-line" role="alert">Shadow phase failed: {{ s?.shadow_error }}</p>
          }
          @if (shadows().length === 0) {
            <app-empty-state
              title="No shadow strategies evaluated"
              message="Shadow strategies run after real trading runs (not dry runs) when shadow mode is on."
            />
          } @else {
            <app-data-table
              caption="How each shadow strategy was evaluated"
              [rows]="shadows()"
              [columns]="shadowColumns"
              [rowKey]="shadowKey"
              [pageSize]="0"
            >
              <ng-template appCell="status" [appCellOf]="shadows()" let-o>
                <app-status-pill [status]="o.status" />
                @if (o.error) {
                  <span class="reason-text">{{ o.error }}</span>
                }
              </ng-template>
            </app-data-table>
          }
        </section>
      </div>
    }
  `,
  styleUrl: './orders-views.scss',
  styles: `
    .run-id {
      flex-basis: 100%;
      font-size: var(--text-xs);
      overflow-wrap: anywhere;
    }
    .reason-text {
      display: block;
      margin-top: 2px;
      font-size: var(--text-xs);
      color: var(--color-loss);
      overflow-wrap: anywhere;
      white-space: normal;
    }
  `,
})
export class TickDetailPage {
  private readonly ticksApi = inject(TicksService);
  private readonly ordersApi = inject(OrdersService);

  /** Route param. */
  readonly id = input.required<string>();

  protected readonly tick = resource({
    params: () => ({ id: this.id() }),
    loader: ({ params }) => this.ticksApi.get(params.id),
  });
  protected readonly fills = resource({
    params: () => ({ tick_id: this.id(), limit: TICK_FILLS_LIMIT }),
    loader: ({ params }) => this.ordersApi.fills(params),
  });
  /** The run's total fill count when this page shows only the first ones. */
  protected readonly fillsCapped = computed(() => {
    if (!this.fills.hasValue()) return null;
    const page = this.fills.value();
    return page.total > page.items.length ? { shown: page.items.length, total: page.total } : null;
  });

  private readonly summary = computed(() =>
    this.tick.hasValue() ? this.tick.value().summary : null,
  );
  protected readonly risks = computed(() => this.summary()?.risk_adjustments ?? []);
  protected readonly shadows = computed(() => this.summary()?.shadow ?? []);
  protected readonly outcome = computed(() => tickOutcome(this.summary()));
  protected readonly notes = computed(() => tickNotes(this.summary()));
  protected readonly took = computed(() =>
    this.tick.hasValue()
      ? formatDuration(this.tick.value().started_at, this.tick.value().finished_at)
      : '–',
  );
  protected readonly expectedReturn = computed(() =>
    formatPercent(this.summary()?.winner_expected_return, { signed: true }),
  );
  protected readonly outcomeDetail = computed(() => {
    const o = this.outcome();
    if (o.kind === 'exit') return 'No candidate qualified';
    if (o.kind === 'winner') return `Expected ${this.expectedReturn()}`;
    return null;
  });
  protected readonly shadowCount = computed(() => {
    const n = this.shadows().length;
    return n ? `${n} shadow strateg${n === 1 ? 'y' : 'ies'} evaluated` : null;
  });

  protected readonly dateTime = formatDateTime;
  protected readonly humanize = humanize;
  protected count(n: number | null | undefined): string {
    return n == null ? '–' : String(n);
  }

  protected readonly riskColumns: TableColumn<RiskAdjustmentView>[] = [
    { key: 'ticker', label: 'Ticker', mobile: 'title' },
    { key: 'side', label: 'Side' },
    { key: 'rule', label: 'Rule', value: (r) => humanize(r.rule) },
    { key: 'original_quantity', label: 'Asked', format: 'number' },
    { key: 'adjusted_quantity', label: 'Allowed', format: 'number' },
    { key: 'reason', label: 'Reason', sortable: false },
  ];
  protected readonly riskKey = (r: RiskAdjustmentView) => `${r.ticker}|${r.side}|${r.rule}`;

  protected readonly fillColumns: TableColumn<FillView>[] = FILL_COLUMNS.filter(
    (c) => c.key !== 'tick_id',
  );
  protected readonly fillKey = (f: FillView) => String(f.id);

  protected readonly shadowColumns: TableColumn<ShadowOutcomeView>[] = [
    { key: 'strategy_id', label: 'Strategy', mobile: 'title' },
    { key: 'status', label: 'Status' },
    { key: 'decisions', label: 'Decisions', format: 'number' },
    { key: 'fills', label: 'Fills', format: 'number' },
    { key: 'total_value', label: 'Value', format: 'money' },
  ];
  protected readonly shadowKey = (o: ShadowOutcomeView) => o.strategy_id;
}
