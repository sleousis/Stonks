import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  resource,
  signal,
} from '@angular/core';

import type { TcaGroupView, TcaSummaryView } from '../../api/models';
import { strategyDisplayName } from '../../shared/strategy-names';
import { TcaService } from '../../api/tca.service';
import { formatMoney, formatNumber, formatPercent } from '../../core/format/format';
import { PortfolioContextService } from '../../core/portfolio/portfolio-context.service';
import { DataTable, type TableColumn } from '../../shared/ui/data-table/data-table';
import { Segmented, type SegmentOption } from '../../shared/ui/segmented';
import { StatTile } from '../../shared/ui/stat-tile';
import { EmptyState, ErrorState, LoadingState } from '../../shared/ui/states';
import { bps1, formatBps, portfolioName } from './trades-format';

export type Grouping = 'strategy' | 'ticker' | 'portfolio';

const GROUPINGS: readonly SegmentOption<Grouping>[] = [
  { value: 'strategy', label: 'By strategy' },
  { value: 'ticker', label: 'By ticker' },
  { value: 'portfolio', label: 'By portfolio' },
];

const KEY_LABELS: Record<Grouping, string> = {
  strategy: 'Strategy',
  ticker: 'Ticker',
  portfolio: 'Portfolio',
};

/**
 * Headline trade costs (implementation shortfall, fees, fill rate, the gap
 * to the cost model) and the same figures broken down by strategy, ticker
 * or portfolio.
 */
@Component({
  selector: 'app-tca-summary',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [DataTable, Segmented, StatTile, EmptyState, ErrorState, LoadingState],
  styleUrl: './trades.scss',
  template: `
    <section class="totals" aria-labelledby="costs-title">
      <h2 id="costs-title" class="visually-hidden">What trading cost</h2>
      @if (totals.error(); as err) {
        <app-error-state
          title="Could not load trade costs"
          [error]="err"
          (retry)="totals.reload()"
        />
      } @else if (!totals.hasValue()) {
        <app-loading-state label="Loading trade costs" [rows]="3" />
      } @else if (headline(); as g) {
        <div class="summary-strip" aria-label="Trade cost totals">
          <app-stat-tile
            label="Total cost"
            [value]="totalCost(g)"
            detail="Shortfall plus missed fills"
            [help]="false"
          />
          <app-stat-tile
            label="Shortfall"
            [value]="bps(g.is_bps)"
            [detail]="money(headlineMoney(g).is_cost, totalsCurrency())"
            [help]="false"
          />
          <app-stat-tile
            label="Fees"
            [value]="bps(g.fee_bps)"
            detail="Part of the shortfall"
            [help]="false"
          />
          <app-stat-tile
            label="Orders filled"
            [value]="count(g.filled_orders) + ' of ' + count(g.orders)"
            [detail]="fillRate(g)"
            [help]="false"
          />
          <app-stat-tile
            label="Versus the cost model"
            [value]="bps(g.model_gap_bps)"
            [detail]="'Expected ' + bps(g.expected_bps)"
            [help]="false"
          />
        </div>
      } @else {
        <div class="none">
          <app-empty-state
            icon="orders"
            title="No trades yet"
            message="Costs appear after the first filled order."
          />
        </div>
        <!-- The shortfall line shows before the first trade too (old tca-summary.ts:50). -->
        <p class="lead">
          Shortfall is the gap between the price when the order was decided and the price actually
          paid, fees included. Positive figures are costs. A negative figure means the price moved
          in your favour.
        </p>
      }
    </section>

    <!-- With no trades yet the totals already say so: no second empty section. -->
    @if (!noTrades()) {
      <div class="cols-facts">
        <section class="page-section section-flush" aria-labelledby="breakdown-title">
          <header class="section-head">
            <h2 id="breakdown-title">Breakdown</h2>
            <app-segmented label="Group costs" [options]="groupings" [(value)]="grouping" />
          </header>
          <div class="section-body">
            @if (breakdown.error(); as err) {
              <app-error-state
                title="Could not load the breakdown"
                [error]="err"
                (retry)="breakdown.reload()"
              />
            } @else if (!breakdown.hasValue()) {
              <app-loading-state label="Loading the breakdown" [rows]="4" />
            } @else if (breakdown.value().groups.length === 0) {
              <app-empty-state
                icon="orders"
                title="No trades yet"
                message="Costs appear after the first filled order."
              />
            } @else {
              @let rows = breakdown.value().groups;
              <app-data-table
                [caption]="'Trade costs ' + groupingLabel().toLowerCase()"
                [rows]="rows"
                [columns]="columns()"
                [rowKey]="rowKey"
                [initialSort]="{ key: 'is_cost', dir: 'desc' }"
              />
            }
          </div>
        </section>
        <aside class="callout reading" aria-label="How to read trade costs">
          <p class="callout-title">How to read this</p>
          <p class="lead">
            Shortfall is the gap between the price when the order was decided and the price actually
            paid, fees included. Positive figures are costs. A negative figure means the price moved
            in your favour.
          </p>
          <p>A basis point (bps) is one hundredth of a percent.</p>
        </aside>
      </div>
    }
  `,
  styles: `
    .breakdown {
      margin-top: var(--space-4);
    }
  `,
})
export class TcaSummary {
  private readonly tca = inject(TcaService);
  private readonly ctx = inject(PortfolioContextService);

  protected readonly groupings = GROUPINGS;
  readonly grouping = signal<Grouping>('strategy');

  protected readonly totals = resource({
    params: () => ({ portfolio: this.ctx.selectedId() }),
    loader: () => this.tca.summary({ by: 'all' }),
  });

  protected readonly breakdown = resource({
    params: () => ({ by: this.grouping(), portfolio: this.ctx.selectedId() }),
    loader: ({ params }) => this.tca.summary({ by: params.by }),
  });

  /** The one "all" group, or null when nothing was ordered yet. */
  protected readonly headline = computed<TcaGroupView | null>(() => {
    if (!this.totals.hasValue()) return null;
    const g = this.totals.value().groups[0];
    return g && g.orders > 0 ? g : null;
  });

  /** The totals loaded and hold no order: nothing to break down. */
  protected readonly noTrades = computed(
    () => this.totals.hasValue() && !this.totals.error() && this.headline() === null,
  );

  protected readonly groupingLabel = computed(
    () => GROUPINGS.find((g) => g.value === this.grouping())?.label ?? '',
  );

  protected readonly columns = computed<TableColumn<TcaGroupView>[]>(() => {
    const by = this.grouping();
    const options = this.ctx.options();
    const view = this.breakdown.hasValue() ? this.breakdown.value() : null;
    const currency = currencyOf(view);
    const inBase = (g: TcaGroupView) => moneyIn(view, g);
    return [
      {
        key: 'key',
        label: KEY_LABELS[by],
        mobile: 'title',
        value: (g) =>
          (by === 'portfolio' ? portfolioName(g.key, options) : null) ??
          (by === 'strategy' && g.key ? strategyDisplayName(g.key) : null) ??
          (g.key || 'None recorded'),
      },
      { key: 'orders', label: 'Orders', format: 'number' },
      { key: 'filled_orders', label: 'Filled', format: 'number', mobile: 'hide' },
      {
        key: 'is_bps',
        label: 'Shortfall (bps)',
        format: 'number',
        value: (g) => bps1(g.is_bps),
      },
      {
        key: 'is_cost',
        label: 'Shortfall',
        format: 'money',
        value: (g) => inBase(g).is_cost,
        currency: () => currency,
      },
      {
        key: 'fee_bps',
        label: 'Fees (bps)',
        format: 'number',
        mobile: 'hide',
        value: (g) => bps1(g.fee_bps),
      },
      {
        key: 'opportunity_cost',
        label: 'Missed fills',
        format: 'money',
        mobile: 'hide',
        value: (g) => inBase(g).opportunity_cost,
        currency: () => currency,
      },
      {
        key: 'model_gap_bps',
        label: 'Versus model (bps)',
        format: 'number',
        mobile: 'hide',
        value: (g) => bps1(g.model_gap_bps),
      },
    ];
  });

  protected readonly rowKey = (g: TcaGroupView) => g.key;

  protected bps(value: number | null | undefined): string {
    return formatBps(value);
  }

  /** The totals' currency: the portfolio's base currency when the server converted. */
  protected readonly totalsCurrency = computed(() =>
    currencyOf(this.totals.hasValue() ? this.totals.value() : null),
  );

  protected headlineMoney(g: TcaGroupView): GroupMoney {
    return moneyIn(this.totals.hasValue() ? this.totals.value() : null, g);
  }

  protected totalCost(g: TcaGroupView): string {
    const m = this.headlineMoney(g);
    const total =
      m.is_cost == null || m.opportunity_cost == null ? null : m.is_cost + m.opportunity_cost;
    return this.money(total, this.totalsCurrency());
  }

  protected money(value: number | null | undefined, currency?: string | null): string {
    return formatMoney(value, { currency });
  }

  protected count(value: number): string {
    return formatNumber(value, { digits: 0 });
  }

  protected fillRate(g: TcaGroupView): string {
    return g.orders > 0 ? `${formatPercent(g.filled_orders / g.orders, { digits: 0 })} filled` : '';
  }
}

interface GroupMoney {
  is_cost: number | null;
  opportunity_cost: number | null;
}

/** The currency of a summary's money: its base currency once the server converts. */
function currencyOf(view: TcaSummaryView | null): string | null {
  return view?.base_currency ?? null;
}

/**
 * A group's money in the base currency (each order converted at its day's
 * rate, null when a rate is missing). An older server without the base
 * figures keeps the raw sums.
 */
function moneyIn(view: TcaSummaryView | null, g: TcaGroupView): GroupMoney {
  if (!view?.base_currency) return { is_cost: g.is_cost, opportunity_cost: g.opportunity_cost };
  const base = (view.groups_base ?? []).find((m) => m.key === g.key);
  return { is_cost: base?.is_cost ?? null, opportunity_cost: base?.opportunity_cost ?? null };
}
