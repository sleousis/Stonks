import { ChangeDetectionStrategy, Component, computed, inject, resource } from '@angular/core';

import { PortfolioService } from '../../api/portfolio.service';
import { formatMoney, formatNumber } from '../../core/format/format';
import { StatTile } from '../../shared/ui/stat-tile';
import { ErrorState, LoadingState } from '../../shared/ui/states';

/** Admins: totals across every trader. Never anyone's holdings. */
@Component({
  selector: 'app-totals-card',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [StatTile, LoadingState, ErrorState],
  template: `
    <section class="panel" aria-labelledby="home-totals">
      <div class="panel-head">
        <h2 id="home-totals">All traders</h2>
      </div>
      @if (totals.error(); as err) {
        <app-error-state title="Could not load totals" [error]="err" (retry)="totals.reload()" />
      } @else if (!totals.hasValue()) {
        <app-loading-state label="Loading totals" [rows]="3" />
      } @else {
        <div class="panel-body body">
          <div class="tiles">
            <app-stat-tile label="Total value" featured [value]="value()" [help]="false" />
            <app-stat-tile label="Cash" [value]="cash()" [help]="false" />
            <app-stat-tile label="Portfolios" [value]="portfolios()" [help]="false" />
            <app-stat-tile label="Traders" [value]="owners()" [help]="false" />
          </div>
          <p class="hint">Sums only. Admins never see anyone's holdings.</p>
        </div>
      }
    </section>
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
    .hint {
      font-size: var(--text-xs);
      color: var(--color-ink-3);
    }
  `,
})
export class TotalsCard {
  private readonly api = inject(PortfolioService);
  protected readonly totals = resource({ loader: () => this.api.totals() });

  protected readonly value = computed(() =>
    this.totals.hasValue() ? formatMoney(this.totals.value().total_value) : '',
  );
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
