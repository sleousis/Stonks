import { ChangeDetectionStrategy, Component, computed, inject, resource } from '@angular/core';

import { PortfolioService } from '../../api/portfolio.service';
import { formatMoney, formatNumber } from '../../core/format/format';
import { StatTile } from '../../shared/ui/stat-tile';
import { ErrorState } from '../../shared/ui/states';

/**
 * Admins: totals across every trader, under their own portfolio on Today.
 * Never anyone's holdings. Too few traders with real money to sum without
 * showing someone's: one quiet line, not an empty panel (M3).
 */
@Component({
  selector: 'app-totals-card',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [StatTile, ErrorState],
  template: `
    @if (suppressed()) {
      <p class="line">
        All traders: totals appear once three or more traders have real money. Paper accounts are
        left out, and sums of fewer people would show theirs.
      </p>
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
        <div class="panel-body body">
          <div class="tiles">
            <app-stat-tile
              label="Total value"
              featured
              [value]="value()"
              [amount]="totalValue()"
              [format]="money"
              [help]="false"
            />
            <app-stat-tile label="Cash" [value]="cash()" [help]="false" />
            <app-stat-tile label="Portfolios" [value]="portfolios()" [help]="false" />
            <app-stat-tile label="Traders" [value]="owners()" [help]="false" />
          </div>
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
    .body {
      display: grid;
      gap: var(--space-3);
    }
    .tiles {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: var(--space-3);
    }
    /* Phones: the headline figure gets the whole width. */
    @media (max-width: 767.98px) {
      .tiles .featured {
        grid-column: 1 / -1;
      }
    }
    .line {
      margin: 0;
      padding: 0 var(--space-1);
      font-size: var(--text-xs);
      color: var(--color-ink-3);
    }
    .hint {
      font-size: var(--text-xs);
      color: var(--color-ink-3);
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
