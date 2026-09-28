import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  input,
  resource,
  signal,
} from '@angular/core';
import { ActivatedRoute, Router, RouterLink } from '@angular/router';

import { FactorsService } from '../../../api/factors.service';
import type { FactorView } from '../../../api/models';
import { DataTable, TableCell, type TableColumn } from '../../../shared/ui/data-table/data-table';
import { keepLatest } from '../../../shared/ui/data-table/keep-latest';
import { PageHeader } from '../../../shared/ui/page-header';
import { EmptyState, ErrorState, LoadingState } from '../../../shared/ui/states';
import { LabNav } from '../lab-nav';
import { FORMULA_ROUTE, directionText, matchesQuery, warmupText } from './factor-requests';

type Kind = 'expression' | 'fundamental';

/**
 * The factor library: every factor Stonks can compute, searchable and
 * filtered by set, family and kind. Open one for its values, a tear sheet
 * and a lab run, or write a formula of your own.
 */
@Component({
  selector: 'app-factors-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    RouterLink,
    PageHeader,
    LabNav,
    DataTable,
    TableCell,
    EmptyState,
    ErrorState,
    LoadingState,
  ],
  template: `
    <app-page-header
      title="Lab"
      description="Factors rank names by a number per day. Test one before any strategy trades it."
    >
      <a actions class="btn" [routerLink]="['/lab/factors', formulaRoute]">Write a formula</a>
    </app-page-header>
    <app-lab-nav />

    <section class="panel" aria-labelledby="factors-title">
      <div class="panel-head">
        <h2 id="factors-title">Factor library</h2>
        @if (lastCatalog(); as c) {
          <span class="count num">{{ shown().length }} of {{ c.factors.length }}</span>
        }
      </div>
      <div class="panel-body">
        <p class="lead">
          A factor is a number per name per day that should rank the returns that follow. Each one
          says whether higher or lower is better and how many bars of history it needs first.
        </p>
        <form
          class="filters"
          role="search"
          aria-label="Filter the factor library"
          (submit)="$event.preventDefault()"
        >
          <div class="field">
            <label for="factor-search">Search</label>
            <input
              id="factor-search"
              class="input"
              type="search"
              autocomplete="off"
              placeholder="mom, volatility, piotroski"
              [value]="query()"
              (input)="query.set($any($event.target).value)"
            />
          </div>
          <div class="field">
            <label for="factor-set">Set</label>
            <select
              id="factor-set"
              class="input"
              (change)="setFilter('set', $any($event.target).value)"
            >
              <option value="" [selected]="!set()">All sets</option>
              @for (s of sets(); track s.name) {
                <option [value]="s.name" [selected]="s.name === set()">
                  {{ s.name }} ({{ s.count }})
                </option>
              }
            </select>
          </div>
          <div class="field">
            <label for="factor-family">Family</label>
            <select
              id="factor-family"
              class="input"
              (change)="setFilter('family', $any($event.target).value)"
            >
              <option value="" [selected]="!family()">All families</option>
              @for (f of families(); track f) {
                <option [value]="f" [selected]="f === family()">{{ f }}</option>
              }
            </select>
          </div>
          <div class="field">
            <label for="factor-kind">Kind</label>
            <select
              id="factor-kind"
              class="input"
              (change)="setFilter('kind', $any($event.target).value)"
            >
              <option value="" [selected]="!kind()">Any kind</option>
              <option value="expression" [selected]="kind() === 'expression'">
                Price and volume formula
              </option>
              <option value="fundamental" [selected]="kind() === 'fundamental'">
                Fundamentals score
              </option>
            </select>
          </div>
        </form>

        @if (catalog.error(); as err) {
          <app-error-state
            title="Could not load the factor library"
            [error]="err"
            (retry)="catalog.reload()"
          />
        } @else if (!lastCatalog()) {
          <app-loading-state label="Loading the factor library" [rows]="8" />
        } @else if (shown().length === 0) {
          <app-empty-state
            title="No factor matches"
            message="Clear the search or pick another set, family or kind. You can also write a formula of your own."
          />
        } @else {
          <app-data-table
            caption="Factors in the library"
            [rows]="shown()"
            [columns]="columns"
            [rowKey]="key"
            [pageSize]="50"
            [busy]="catalog.isLoading()"
          >
            <ng-template appCell="id" [appCellOf]="shown()" let-f>
              <a class="cell-link mono" [routerLink]="['/lab/factors', f.id]">{{ f.id }}</a>
            </ng-template>
          </app-data-table>
        }
      </div>
    </section>
  `,
  styleUrl: '../ledger.page.scss',
  styles: `
    @use 'breakpoints' as bp;
    .filters {
      @include bp.from-tablet {
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }
      @include bp.from-desktop {
        grid-template-columns: minmax(0, 2fr) repeat(3, minmax(0, 1fr));
      }
    }
    .mono {
      font-family: var(--font-mono);
      overflow-wrap: anywhere;
    }
  `,
})
export class FactorsPage {
  private readonly factors = inject(FactorsService);
  private readonly router = inject(Router);
  private readonly route = inject(ActivatedRoute);

  /** `?set=`, `?family=` and `?kind=` narrow the list on the server. */
  readonly set = input<string>();
  readonly family = input<string>();
  readonly kind = input<string>();

  protected readonly formulaRoute = FORMULA_ROUTE;
  protected readonly query = signal('');

  protected readonly catalog = resource({
    params: () => ({
      set: this.set() || null,
      family: this.family() || null,
      kind: (this.kind() === 'expression' || this.kind() === 'fundamental'
        ? this.kind()
        : null) as Kind | null,
    }),
    loader: ({ params }) => this.factors.list(params),
  });

  /** Sets and families from the last good load, so the filters stay while one loads. */
  protected readonly lastCatalog = keepLatest(this.catalog);
  protected readonly sets = computed(() => this.lastCatalog()?.sets ?? []);
  protected readonly families = computed(() => this.lastCatalog()?.families ?? []);

  protected readonly shown = computed(() =>
    (this.lastCatalog()?.factors ?? []).filter((f) => matchesQuery(f, this.query())),
  );

  protected readonly key = (f: FactorView) => f.id;
  protected readonly columns: TableColumn<FactorView>[] = [
    { key: 'id', label: 'Factor', mobile: 'title', help: false },
    { key: 'description', label: 'What it measures', sortable: false, help: false },
    { key: 'set', label: 'Set', value: (f) => f.set ?? 'Formula', help: false },
    { key: 'family', label: 'Family', help: false },
    {
      key: 'direction',
      label: 'Better when',
      value: (f) => directionText(f.direction).replace(' is better', ''),
      mobile: 'hide',
      help: false,
    },
    {
      key: 'lookback_bars',
      label: 'Warm-up',
      align: 'end',
      value: (f) => warmupText(f.lookback_bars),
      mobile: 'hide',
      help: false,
    },
  ];

  protected setFilter(name: 'set' | 'family' | 'kind', value: string): void {
    void this.router.navigate([], {
      relativeTo: this.route,
      queryParams: { [name]: value || null },
      queryParamsHandling: 'merge',
      replaceUrl: true,
    });
  }
}
