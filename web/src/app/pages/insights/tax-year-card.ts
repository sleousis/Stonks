import { ChangeDetectionStrategy, Component, inject, input, resource } from '@angular/core';

import { TaxService } from '../../api/tax.service';
import { formatMoney, formatNumber, formatPercent, toneClass } from '../../core/format/format';
import { ErrorState, LoadingState } from '../../shared/ui/states';

/**
 * The tax owed so far this year: gains realised in the portfolio, short and
 * long term, and the estimated tax at the configured rates, in the base
 * currency. An estimate, not tax advice.
 */
@Component({
  selector: 'app-tax-year-card',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ErrorState, LoadingState],
  template: `
    <section class="panel" aria-labelledby="tax-year-title">
      <div class="panel-head">
        <h2 id="tax-year-title">Tax owed this year</h2>
      </div>
      <div class="panel-body">
        @if (year.error(); as err) {
          <app-error-state
            title="Could not estimate this year's tax"
            [error]="err"
            (retry)="year.reload()"
          />
        } @else if (!year.hasValue()) {
          <app-loading-state label="Estimating this year's tax" [rows]="2" />
        } @else {
          @let y = year.value();
          <p class="owed">
            <span class="num big">{{ money(y.estimated_tax, y.base_currency) }}</span>
            <span class="muted">estimated on {{ num(y.disposals) }} sales in {{ y.year }}</span>
          </p>
          <dl class="sums">
            <div>
              <dt>Short term gain</dt>
              <dd class="num" [class]="tone(y.short_term_gain)">
                {{ money(y.short_term_gain, y.base_currency) }}
              </dd>
            </div>
            <div>
              <dt>Long term gain</dt>
              <dd class="num" [class]="tone(y.long_term_gain)">
                {{ money(y.long_term_gain, y.base_currency) }}
              </dd>
            </div>
            <div>
              <dt>Rates</dt>
              <dd class="num">{{ pct(y.rates.short_term) }} / {{ pct(y.rates.long_term) }}</dd>
            </div>
          </dl>
          @if (y.unconverted > 0) {
            <p class="muted">
              {{ num(y.unconverted) }} sales are left out: no exchange rate converts them.
            </p>
          }
          <p class="muted">An estimate at the rates in your settings, not tax advice.</p>
        }
      </div>
    </section>
  `,
  styles: `
    .owed {
      display: flex;
      flex-wrap: wrap;
      align-items: baseline;
      gap: var(--space-2);
    }
    .big {
      font-size: var(--text-xl);
      font-weight: var(--weight-bold);
    }
    .sums {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(9rem, 1fr));
      gap: var(--space-2);
      margin: var(--space-3) 0;
    }
    .sums dt {
      color: var(--color-ink-2);
      font-size: var(--text-sm);
    }
    .gain {
      color: var(--color-gain);
    }
    .loss {
      color: var(--color-loss);
    }
  `,
})
export class TaxYearCard {
  private readonly api = inject(TaxService);

  /** The portfolio shown; a change reloads the estimate. */
  readonly portfolio = input<string | null>(null);

  protected readonly year = resource({
    params: () => ({ portfolio: this.portfolio() }),
    loader: () => this.api.year(),
  });

  protected readonly tone = toneClass;
  protected num(v: number): string {
    return formatNumber(v);
  }
  protected pct(v: number): string {
    return formatPercent(v, { digits: 0 });
  }
  protected money(v: number, currency: string): string {
    return formatMoney(v, { currency });
  }
}
