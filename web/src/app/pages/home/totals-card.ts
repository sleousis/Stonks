import { ChangeDetectionStrategy, Component, computed, inject, resource } from '@angular/core';

import { PortfolioService } from '../../api/portfolio.service';
import { formatMoney, formatNumber } from '../../core/format/format';
import { StatTile } from '../../shared/ui/stat-tile';
import { ErrorState } from '../../shared/ui/states';

/**
 * Admins: totals across every trader, under their own portfolio on Today.
 * Never anyone's holdings. Too few traders with real money to sum without
 * showing someone's: nothing at all here, not an empty panel (M3). Insights
 * says once why the totals are held back.
 */
@Component({
  selector: 'app-totals-card',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [StatTile, ErrorState],
  template: `
    @if (suppressed()) {
      <!-- Held back: Today stays quiet, Insights explains. -->
    } @else if (totals.error(); as err) {
      <section class="panel" aria-labelledby="home-totals">
        <div class="panel-head">
          <h2 id="home-totals">All traders</h2>
        </div>
        <app-error-state title="Could not load totals" [error]="err" (retry)="totals.reload()" />
      </section>
    } @else if (totals.hasValue()) {
      <section class="panel" aria-labelledby="home-totals">
        <div class="panel-head">
          <h2 id="home-totals">All traders</h2>
        </div>
        <div class="stat-strip tiles">
          <app-stat-tile
            bare
            label="Total value"
            [value]="value()"
            [amount]="totalValue()"
            [format]="money"
            [help]="false"
          />
          <app-stat-tile bare label="Cash" [value]="cash()" [help]="false" />
          <app-stat-tile bare label="Portfolios" [value]="portfolios()" [help]="false" />
          <app-stat-tile bare label="Traders" [value]="owners()" [help]="false" />
        </div>
        <div class="panel-body">
          <p class="hint">Sums only. Admins never see anyone's holdings.</p>
        </div>
      </section>
    }
  `,
  styles: `
    :host {
      display: block;
      min-width: 0;
    }
    :host:empty {
      display: none;
    }
    /* Sums in one divided strip: the page's one headline figure stays in My portfolio. */
    .tiles {
      grid-template-columns: repeat(auto-fit, minmax(8rem, 1fr));
      border-bottom: 1px solid var(--rule);
    }
    .hint {
      font-size: var(--text-xs);
      color: var(--muted);
    }
  `,
})
export class TotalsCard {
  private readonly api = inject(PortfolioService);
  protected readonly totals = resource({ loader: () => this.api.totals() });
  /** Too few live owners to sum without showing someone's money: the API sends zeros. */
  protected readonly suppressed = computed(
    () => this.totals.hasValue() && this.totals.value().suppressed === true,
  );

  protected readonly value = computed(() =>
    this.totals.hasValue() ? formatMoney(this.totals.value().total_value) : '',
  );
  protected readonly totalValue = computed(() =>
    this.totals.hasValue() ? this.totals.value().total_value : null,
  );
  protected readonly money = (n: number) => formatMoney(n);
  protected readonly cash = computed(() =>
    this.totals.hasValue() ? formatMoney(this.totals.value().cash) : '',
  );
  protected readonly portfolios = computed(() =>
    this.totals.hasValue() ? formatNumber(this.totals.value().portfolios) : '',
  );
  protected readonly owners = computed(() =>
    this.totals.hasValue() ? formatNumber(this.totals.value().owners) : '',
  );
}
