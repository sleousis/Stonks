import { ChangeDetectionStrategy, Component, inject, input, resource } from '@angular/core';

import { TaxService } from '../../api/tax.service';
import { formatMoney, formatNumber, formatPercent, toneClass } from '../../core/format/format';
import { ErrorState, LoadingState } from '../../shared/ui/states';

/**
 * The tax owed so far this year (on a paper portfolio, what it would be):
 * gains realised in the portfolio, short and long term, and the estimated
 * tax at the configured rates, in the base currency. An estimate, not tax
 * advice.
 */
@Component({
  selector: 'app-tax-year-card',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ErrorState, LoadingState],
  template: `
    <section class="page-section" aria-labelledby="tax-year-title">
      <div class="section-head">
        <h2 id="tax-year-title">{{ paper() ? 'Tax if this were real' : 'Tax owed this year' }}</h2>
      </div>
      <div class="section-body">
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
            <span class="big headline-figure">{{ money(y.estimated_tax, y.base_currency) }}</span>
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
              <dt>Tax rates</dt>
              <dd>
                Short term {{ pct(y.rates.short_term) }}, long term {{ pct(y.rates.long_term) }}
              </dd>
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
      font-size: 40px;
    }
    .owed {
      margin-bottom: var(--space-4);
    }
    .sums {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(10rem, 1fr));
      margin: 0 0 var(--space-3);
      border-top: 1px solid var(--rule);
      border-bottom: 1px solid var(--rule);
    }
    .sums > div {
      padding: var(--space-3) var(--space-4) var(--space-3) 0;
    }
    .sums dt {
      color: var(--muted);
      font-size: var(--text-xs);
    }
    .sums dd {
      margin: var(--space-1) 0 0;
      font-size: var(--text-lead);
      font-weight: var(--weight-semibold);
    }
    .gain {
      color: var(--up);
    }
    .loss {
      color: var(--down);
    }
  `,
})
export class TaxYearCard {
  private readonly api = inject(TaxService);

  /** The portfolio shown; a change reloads the estimate. */
  readonly portfolio = input<string | null>(null);
  /** Simulated money: the figure is what the tax would be, not what is owed. */
  readonly paper = input(false);

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
