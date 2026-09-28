import { ChangeDetectionStrategy, Component, computed, inject, input, signal } from '@angular/core';

import { FactorsService } from '../../../api/factors.service';
import type {
  FactorValue,
  FactorValuesView,
  UniverseView,
  WatchlistView,
} from '../../../api/models';
import { formatNumber } from '../../../core/format/format';
import { DataTable, type TableColumn } from '../../../shared/ui/data-table/data-table';
import { EmptyState, ErrorState } from '../../../shared/ui/states';
import { defaultWindow } from '../lab-requests';
import { BasketPicker } from './basket-picker';
import {
  type Basket,
  basketError,
  buildValuesRequest,
  defaultBasket,
  directionText,
} from './factor-requests';

/**
 * A factor's value for every name of a universe, watchlist or typed list,
 * known at the close of a date, best first in the factor's direction.
 */
@Component({
  selector: 'app-factor-values',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [BasketPicker, DataTable, EmptyState, ErrorState],
  template: `
    <form class="form" novalidate (submit)="$event.preventDefault(); show()">
      <app-basket-picker
        idPrefix="fv"
        [basket]="basket()"
        [universes]="universes()"
        [watchlists]="watchlists()"
        [error]="tried() ? basketMessage() : null"
        (basketChange)="basket.set($event)"
      />
      <div class="row">
        <div class="field">
          <label for="fv-date">On the close of</label>
          <input
            id="fv-date"
            class="input"
            type="date"
            [value]="asOf()"
            [attr.aria-invalid]="tried() && !asOf()"
            (input)="asOf.set($any($event.target).value)"
          />
        </div>
        <button class="btn" type="submit" [disabled]="loading() || !factor()">
          {{ loading() ? 'Loading values' : 'Show values' }}
        </button>
      </div>
    </form>

    @if (error(); as err) {
      <app-error-state title="Could not load the values" [error]="err" (retry)="show()" />
    } @else if (view(); as v) {
      @if (v.values.length === 0) {
        <app-empty-state
          title="No values on this date"
          message="None of these names had enough history on that date. Pick a later date or more names."
        />
      } @else {
        <p class="muted small">
          {{ v.values.length }} names on {{ v.as_of }}, best first. {{ direction(v.direction) }}.
        </p>
        <app-data-table
          [caption]="'Values of ' + v.factor_id + ' on ' + v.as_of"
          [rows]="v.values"
          [columns]="columns"
          [rowKey]="key"
          [pageSize]="25"
          [initialSort]="{ key: 'rank', dir: 'asc' }"
        />
      }
      @if (v.missing.length) {
        <p class="muted small missing">
          No value for {{ v.missing.length }} {{ v.missing.length === 1 ? 'name' : 'names' }}:
          {{ missingText() }}
        </p>
      }
    }
  `,
  styles: `
    :host {
      display: grid;
      gap: var(--space-4);
      min-width: 0;
    }
    .form {
      display: grid;
      gap: var(--space-3);
    }
    .row {
      display: flex;
      flex-wrap: wrap;
      align-items: flex-end;
      gap: var(--space-3);
    }
    .small {
      margin: 0;
      font-size: var(--text-sm);
    }
    .missing {
      overflow-wrap: anywhere;
    }
  `,
})
export class FactorValues {
  private readonly factors = inject(FactorsService);

  /** A library id or a checked formula. */
  readonly factor = input.required<string>();
  readonly universes = input<readonly UniverseView[]>([]);
  readonly watchlists = input<readonly WatchlistView[]>([]);

  protected readonly basket = signal<Basket>(defaultBasket());
  protected readonly asOf = signal(defaultWindow().end);
  protected readonly tried = signal(false);
  protected readonly loading = signal(false);
  protected readonly error = signal<unknown>(null);
  protected readonly view = signal<FactorValuesView | null>(null);

  protected readonly basketMessage = computed(() => basketError(this.basket(), this.watchlists()));
  protected readonly missingText = computed(() => {
    const missing = this.view()?.missing ?? [];
    const shown = missing.slice(0, 20).join(', ');
    return missing.length > 20 ? `${shown} and ${missing.length - 20} more` : shown;
  });

  protected readonly key = (v: FactorValue) => v.ticker;
  protected readonly direction = directionText;
  protected readonly columns: TableColumn<FactorValue>[] = [
    { key: 'rank', label: 'Rank', format: 'number', help: false },
    { key: 'ticker', label: 'Ticker', mobile: 'title', help: false },
    {
      key: 'value',
      label: 'Value',
      align: 'end',
      help: false,
      sortable: false,
      value: (v) => formatNumber(v.value, { digits: 4 }),
    },
  ];

  async show(): Promise<void> {
    this.tried.set(true);
    if (this.basketMessage() || !this.asOf() || !this.factor()) return;
    this.loading.set(true);
    this.error.set(null);
    try {
      this.view.set(
        await this.factors.values(
          buildValuesRequest(this.factor(), this.asOf(), this.basket(), this.watchlists()),
        ),
      );
    } catch (err) {
      this.view.set(null);
      this.error.set(err);
    } finally {
      this.loading.set(false);
    }
  }
}
