import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  inject,
  input,
  output,
  resource,
  signal,
} from '@angular/core';

import type { InstrumentView, ListInstrumentsData } from '../../api/models';
import { MarketService } from '../../api/market.service';
import { DataTable, TableCell, type TableColumn } from '../../shared/ui/data-table/data-table';
import { EmptyState, ErrorState, LoadingState } from '../../shared/ui/states';

type AssetClass = NonNullable<NonNullable<ListInstrumentsData['query']>['asset_class']>;

export const ASSET_CLASSES: readonly AssetClass[] = ['equity', 'crypto', 'commodity', 'bond'];
const PAGE_SIZE = 10;
const DEBOUNCE_MS = 250;

/** Search instruments by id or name and asset class; picking one selects it for the page. */
@Component({
  selector: 'app-instrument-search',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [DataTable, TableCell, LoadingState, EmptyState, ErrorState],
  styleUrls: ['./data-shared.scss'],
  template: `
    <section class="panel" aria-labelledby="search-title">
      <div class="panel-head">
        <h2 id="search-title">Instruments</h2>
        @if (list.hasValue()) {
          <span class="muted num count">{{ list.value().total }} found</span>
        }
      </div>
      <div class="filters" role="search">
        <div class="field">
          <label for="instrument-q">Ticker or name</label>
          <input
            id="instrument-q"
            class="input"
            type="search"
            placeholder="e.g. AAPL or Apple"
            autocomplete="off"
            spellcheck="false"
            maxlength="100"
            [value]="text()"
            (input)="onInput($any($event.target).value)"
          />
        </div>
        <div class="field">
          <label for="instrument-class">Asset class</label>
          <select
            id="instrument-class"
            class="input"
            [value]="assetClass()"
            (change)="setClass($any($event.target).value)"
          >
            <option value="">All classes</option>
            @for (c of assetClasses; track c) {
              <option [value]="c">{{ c }}</option>
            }
          </select>
        </div>
      </div>
      @if (list.error(); as err) {
        <app-error-state
          title="Could not search instruments"
          [error]="err"
          (retry)="list.reload()"
        />
      } @else if (!list.hasValue()) {
        <app-loading-state label="Searching instruments" [rows]="5" />
      } @else if (list.value().items.length === 0) {
        <app-empty-state
          title="No instruments match"
          [message]="
            q() || assetClass()
              ? 'Try a shorter search or another asset class.'
              : 'Instruments appear after a metadata or prices ingest adds them to the lake.'
          "
        />
      } @else {
        <app-data-table
          caption="Instruments in the lake; choose one to see its coverage and prices"
          [rows]="list.value().items"
          [columns]="columns"
          [rowKey]="key"
          [total]="list.value().total"
          [pageSize]="pageSize"
          (pageChange)="offset.set($event.offset)"
        >
          <ng-template appCell="id" [appCellOf]="list.value().items" let-row>
            <button
              type="button"
              class="pick"
              [attr.aria-pressed]="selected() === row.id"
              [attr.aria-label]="'Show ' + row.id"
              (click)="picked.emit(row.id)"
            >
              {{ row.id }}
            </button>
          </ng-template>
        </app-data-table>
      }
    </section>
  `,
})
export class InstrumentSearch {
  private readonly market = inject(MarketService);

  /** The page's current ticker (highlighted in the list). */
  readonly selected = input<string | null>(null);
  readonly picked = output<string>();

  protected readonly assetClasses = ASSET_CLASSES;
  protected readonly pageSize = PAGE_SIZE;
  protected readonly text = signal('');
  protected readonly q = signal('');
  protected readonly assetClass = signal<AssetClass | ''>('');
  protected readonly offset = signal(0);

  protected readonly list = resource({
    params: () => ({
      q: this.q() || undefined,
      asset_class: this.assetClass() || undefined,
      limit: PAGE_SIZE,
      offset: this.offset(),
    }),
    loader: ({ params }) => this.market.instruments(params),
  });

  protected readonly columns: TableColumn<InstrumentView>[] = [
    { key: 'id', label: 'Ticker', mobile: 'title' },
    { key: 'name', label: 'Name' },
    { key: 'asset_class', label: 'Class' },
    { key: 'exchange', label: 'Exchange', mobile: 'hide' },
    { key: 'currency', label: 'Currency', mobile: 'hide' },
  ];
  protected readonly key = (r: InstrumentView) => r.id;

  private timer: ReturnType<typeof setTimeout> | undefined;

  constructor() {
    inject(DestroyRef).onDestroy(() => clearTimeout(this.timer));
  }

  protected onInput(value: string): void {
    this.text.set(value);
    clearTimeout(this.timer);
    this.timer = setTimeout(() => {
      this.q.set(value.trim());
      this.offset.set(0);
    }, DEBOUNCE_MS);
  }

  protected setClass(value: string): void {
    this.assetClass.set(ASSET_CLASSES.includes(value as AssetClass) ? (value as AssetClass) : '');
    this.offset.set(0);
  }

  reload(): void {
    this.list.reload();
  }
}
