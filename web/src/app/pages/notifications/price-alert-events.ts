import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  input,
  linkedSignal,
  resource,
  signal,
} from '@angular/core';
import { RouterLink } from '@angular/router';

import type { PriceAlertEventView, PriceAlertView, WatchlistView } from '../../api/models';
import { PriceAlertsService } from '../../api/price-alerts.service';
import { DataTable, TableCell, type TableColumn } from '../../shared/ui/data-table/data-table';
import { keepLatest } from '../../shared/ui/data-table/keep-latest';
import { EmptyState, ErrorState, LoadingState } from '../../shared/ui/states';
import { alertTitle } from './price-alert-text';

const PAGE_SIZE = 25;

/**
 * When your price alerts fired, newest first, one page at a time. Filter to
 * one alert. Each firing links to the ticker's chart.
 */
@Component({
  selector: 'app-price-alert-events',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink, DataTable, TableCell, LoadingState, EmptyState, ErrorState],
  template: `
    <section class="panel" aria-labelledby="alert-events-title">
      <div class="panel-head">
        <h2 id="alert-events-title">When they fired</h2>
        <div class="field filter">
          <label for="pa-events-rule" class="visually-hidden">Show firings of</label>
          <select id="pa-events-rule" class="input" (change)="rule.set($any($event.target).value)">
            <option value="" [selected]="!rule()">Every alert</option>
            @for (a of alerts(); track a.id) {
              <option [value]="a.id" [selected]="a.id === rule()">{{ title(a) }}</option>
            }
          </select>
        </div>
      </div>
      @let p = page();
      @if (events.error(); as err) {
        <app-error-state title="Could not load firings" [error]="err" (retry)="events.reload()" />
      } @else if (!p) {
        <app-loading-state label="Loading firings" [rows]="3" />
      } @else if (p.items.length === 0) {
        <app-empty-state
          title="No firings yet"
          message="Alerts are checked after each day's prices arrive. A firing shows here and in your feed."
        />
      } @else {
        @for (k of [rule()]; track k) {
          <app-data-table
            caption="Price alert firings, newest first"
            [rows]="p.items"
            [columns]="columns"
            [rowKey]="key"
            [total]="p.total"
            [offset]="p.offset"
            [pageSize]="pageSize"
            [busy]="events.isLoading()"
            (pageChange)="offset.set($event.offset)"
          >
            <ng-template appCell="ticker" [appCellOf]="p.items" let-e>
              <a class="cell-link" [routerLink]="['/charts', e.ticker]">{{ e.ticker }}</a>
            </ng-template>
          </app-data-table>
        }
      }
    </section>
  `,
  styles: `
    :host {
      display: block;
      min-width: 0;
    }
    .panel-head {
      flex-wrap: wrap;
      gap: var(--space-2);
    }
    .filter {
      min-width: 12rem;
    }
  `,
})
export class PriceAlertEvents {
  private readonly api = inject(PriceAlertsService);

  readonly alerts = input<readonly PriceAlertView[]>([]);
  readonly watchlists = input<readonly WatchlistView[]>([]);
  /** Bump to read the first page again (after an alert changes). */
  readonly refresh = input(0);

  protected readonly pageSize = PAGE_SIZE;
  protected readonly rule = signal('');
  protected readonly offset = linkedSignal({
    source: () => [this.rule(), this.refresh()],
    computation: () => 0,
  });
  protected readonly events = resource({
    params: () => ({
      rule_id: this.rule() || null,
      limit: PAGE_SIZE,
      offset: this.offset(),
      refresh: this.refresh(),
    }),
    loader: ({ params }) =>
      this.api.events({ rule_id: params.rule_id, limit: params.limit, offset: params.offset }),
  });
  protected readonly page = keepLatest(this.events);

  private readonly names = computed(() => {
    const out = new Map<string, string>();
    for (const a of this.alerts()) out.set(a.id, alertTitle(a, this.watchlists()));
    return out;
  });

  protected readonly columns: TableColumn<PriceAlertEventView>[] = [
    { key: 'ticker', label: 'Ticker', mobile: 'title' },
    { key: 'price', label: 'Price', format: 'number' },
    { key: 'detail', label: 'What happened' },
    {
      key: 'rule_id',
      label: 'Alert',
      value: (e) => this.names().get(e.rule_id) ?? 'A deleted alert',
      mobile: 'hide',
    },
    { key: 'observed_at', label: 'Price of', format: 'date' },
    { key: 'created_at', label: 'Fired', format: 'datetime', mobile: 'hide' },
  ];
  protected readonly key = (e: PriceAlertEventView) => String(e.id);
  protected title(a: PriceAlertView): string {
    return alertTitle(a, this.watchlists());
  }
}
