import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  resource,
  signal,
} from '@angular/core';

import type { TcaGroupView } from '../../api/models';
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
    <section class="panel" aria-labelledby="costs-title">
      <div class="panel-head">
        <h2 id="costs-title">What trading cost</h2>
      </div>
      <p class="lead">
        Shortfall is the gap between the price when the order was decided and the price actually
        paid, fees included. Positive figures are costs. A negative figure means the price moved in
        your favour.
      </p>

      @if (totals.error(); as err) {
        <app-error-state
          title="Could not load trade costs"
          [error]="err"
          (retry)="totals.reload()"
        />
      } @else if (!totals.hasValue()) {
        <app-loading-state label="Loading trade costs" [rows]="3" />
      } @else if (headline(); as g) {
        <div class="tiles" aria-label="Trade cost totals">
          <app-stat-tile
            label="Total cost"
            [value]="money(g.is_cost + g.opportunity_cost)"
            detail="Shortfall plus missed fills"
            [help]="false"
            featured
          />
          <app-stat-tile
            label="Shortfall"
            [value]="bps(g.is_bps)"
            [detail]="money(g.is_cost)"
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
        <app-empty-state
          title="No trades yet"
          message="Costs appear after the first filled order."
        />
      }
    </section>

    <!-- With no trades yet the totals already say so: no second empty panel. -->
    @if (!noTrades()) {
      <section class="panel breakdown" aria-labelledby="breakdown-title">
        <div class="panel-head">
          <h2 id="breakdown-title">Breakdown</h2>
          <app-segmented label="Group costs" [options]="groupings" [(value)]="grouping" />
        </div>

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
      </section>
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
      { key: 'is_cost', label: 'Shortfall', format: 'money' },
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

  protected money(value: number | null | undefined): string {
    return formatMoney(value);
  }

  protected count(value: number): string {
    return formatNumber(value, { digits: 0 });
  }

  protected fillRate(g: TcaGroupView): string {
    return g.orders > 0 ? `${formatPercent(g.filled_orders / g.orders, { digits: 0 })} filled` : '';
  }
}
