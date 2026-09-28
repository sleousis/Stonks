import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  input,
  linkedSignal,
  resource,
} from '@angular/core';
import { RouterLink } from '@angular/router';

import { FactorsService } from '../../../api/factors.service';
import { UniversesService } from '../../../api/universes.service';
import { WatchlistsService } from '../../../api/watchlists.service';
import { PageHeader } from '../../../shared/ui/page-header';
import { ErrorState, LoadingState } from '../../../shared/ui/states';
import { LabNav } from '../lab-nav';
import { FactorLabRun } from './factor-lab-run';
import { FORMULA_ROUTE, directionText, warmupText } from './factor-requests';
import { FactorTearsheet } from './factor-tearsheet';
import { FactorValues } from './factor-values';
import { FormulaEditor } from './formula-editor';

/**
 * One factor, or a formula of your own (`/lab/factors/formula?expression=`):
 * what it measures, then its values on a date, a tear sheet and a lab run
 * of the `factor` strategy on it.
 */
@Component({
  selector: 'app-factor-detail-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    RouterLink,
    PageHeader,
    LabNav,
    FormulaEditor,
    FactorValues,
    FactorTearsheet,
    FactorLabRun,
    ErrorState,
    LoadingState,
  ],
  template: `
    <app-page-header
      title="Lab"
      description="Check a factor on today’s names, test how well it ranked returns, then test it as a strategy."
    />
    <app-lab-nav />

    <a class="back" routerLink="/lab/factors">Back to the factor library</a>

    @if (isFormula()) {
      <section class="panel" aria-labelledby="formula-title">
        <div class="panel-head">
          <h2 id="formula-title">Your formula</h2>
        </div>
        <div class="panel-body">
          <p class="lead">
            Write a factor in the expression language. It is checked as you type: it must read only
            the past and compare prices as ratios.
          </p>
          <app-formula-editor [initial]="expression() ?? ''" (valid)="checked.set($event)" />
        </div>
      </section>
    } @else if (info.error(); as err) {
      <app-error-state title="Could not load this factor" [error]="err" (retry)="info.reload()" />
    } @else if (!info.hasValue()) {
      <app-loading-state label="Loading the factor" [rows]="4" />
    } @else {
      @let f = info.value();
      <section class="panel" aria-labelledby="factor-title">
        <div class="panel-head">
          <h2 id="factor-title" class="mono">{{ f.id }}</h2>
          <span class="tag">{{ f.set ?? 'Formula' }}</span>
        </div>
        <div class="panel-body">
          <p class="lead">{{ f.description }}</p>
          <dl class="facts">
            <div>
              <dt>Why it should work</dt>
              <dd>{{ f.hypothesis || 'None written' }}</dd>
            </div>
            <div>
              <dt>Better when</dt>
              <dd>{{ direction(f.direction) }}</dd>
            </div>
            <div>
              <dt>Family</dt>
              <dd>{{ f.family }}</dd>
            </div>
            <div>
              <dt>Warm-up</dt>
              <dd class="num">{{ warmup(f.lookback_bars) }}</dd>
            </div>
            <div>
              <dt>Works on</dt>
              <dd>{{ f.asset_classes.join(', ') || 'Any asset class' }}</dd>
            </div>
            <div>
              <dt>Kind</dt>
              <dd>
                {{ f.kind === 'fundamental' ? 'Fundamentals score' : 'Price and volume formula' }}
              </dd>
            </div>
          </dl>
          @if (f.expression) {
            <div class="formula">
              <span class="label">Formula</span>
              <code class="mono">{{ f.expression }}</code>
              <a
                class="btn btn-ghost"
                [routerLink]="['/lab/factors', formulaRoute]"
                [queryParams]="{ expression: f.expression }"
                >Edit as a formula</a
              >
            </div>
          }
        </div>
      </section>
    }

    @if (subject(); as s) {
      <section class="panel" aria-labelledby="values-title">
        <div class="panel-head">
          <h2 id="values-title">Values on a date</h2>
        </div>
        <div class="panel-body">
          <app-factor-values
            [factor]="s"
            [universes]="universeList()"
            [watchlists]="watchlistList()"
          />
        </div>
      </section>

      <section class="panel" aria-labelledby="tearsheet-title">
        <div class="panel-head">
          <h2 id="tearsheet-title">Tear sheet</h2>
        </div>
        <div class="panel-body">
          <p class="lead">
            How well the factor ranked the returns that followed, per look-ahead, per bucket and per
            group. Research only: nothing trades.
          </p>
          <app-factor-tearsheet
            [factor]="s"
            [universes]="universeList()"
            [watchlists]="watchlistList()"
          />
        </div>
      </section>

      <section class="panel" aria-labelledby="run-title">
        <div class="panel-head">
          <h2 id="run-title">Test as a strategy</h2>
        </div>
        <div class="panel-body">
          <p class="lead">
            A lab run of the factor strategy: it holds the top slice of the names by this factor,
            equal weight, rebalanced on month ends. The lab tunes the slice and runs the checks you
            pick. Nothing goes on trial from here.
          </p>
          <app-factor-lab-run
            [factor]="s"
            [info]="isFormula() || !info.hasValue() ? null : info.value()"
            [universes]="universeList()"
            [watchlists]="watchlistList()"
          />
        </div>
      </section>
    } @else if (isFormula()) {
      <p class="muted waiting">
        Values, the tear sheet and a lab run open once the formula checks out.
      </p>
    }
  `,
  styleUrl: '../ledger.page.scss',
  styles: `
    @use 'breakpoints' as bp;
    :host {
      display: grid;
      gap: var(--space-4);
    }
    .back {
      margin-bottom: 0;
    }
    .mono {
      font-family: var(--font-mono);
      overflow-wrap: anywhere;
    }
    .tag {
      padding: 0 var(--space-2);
      border: 1px solid var(--color-border);
      border-radius: var(--radius-sm);
      font-size: var(--text-xs);
      color: var(--color-ink-2);
    }
    .formula {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: var(--space-2) var(--space-3);
      padding: var(--space-3);
      border-radius: var(--radius-sm);
      background: var(--color-surface-2);
      .label {
        font-size: var(--text-xs);
        color: var(--color-ink-2);
      }
      code {
        flex: 1 1 16rem;
        min-width: 0;
      }
      .btn {
        @include bp.phone {
          min-height: var(--touch-min);
        }
      }
    }
    .waiting {
      margin: 0;
    }
  `,
})
export class FactorDetailPage {
  private readonly factors = inject(FactorsService);
  private readonly universesApi = inject(UniversesService);
  private readonly watchlistsApi = inject(WatchlistsService);

  readonly factorId = input.required<string>();
  /** The formula to start from, on `/lab/factors/formula`. */
  readonly expression = input<string>();

  protected readonly formulaRoute = FORMULA_ROUTE;
  protected readonly direction = directionText;
  protected readonly warmup = warmupText;

  protected readonly isFormula = computed(() => this.factorId() === FORMULA_ROUTE);

  protected readonly info = resource({
    params: () => (this.isFormula() ? undefined : { id: this.factorId() }),
    loader: ({ params }) => this.factors.get(params.id),
  });

  /** The formula once it checks out (reset when the page opens another). */
  protected readonly checked = linkedSignal<string | undefined, string | null>({
    source: () => this.expression(),
    computation: () => null,
  });

  /** What the tools below work on: the library id, or the checked formula. */
  protected readonly subject = computed(() => {
    if (this.isFormula()) return this.checked();
    return this.info.hasValue() ? this.info.value().id : null;
  });

  private readonly universes = resource({ loader: () => this.universesApi.list() });
  private readonly watchlists = resource({ loader: () => this.watchlistsApi.list(true) });
  protected readonly universeList = computed(() =>
    this.universes.hasValue() ? this.universes.value() : [],
  );
  protected readonly watchlistList = computed(() =>
    this.watchlists.hasValue() ? this.watchlists.value() : [],
  );
}
