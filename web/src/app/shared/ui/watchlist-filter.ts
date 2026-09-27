import { ChangeDetectionStrategy, Component, inject, input } from '@angular/core';

import { SessionService } from '../../core/auth/session.service';
import { WatchlistContextService } from '../../core/watchlists/watchlist-context.service';

/**
 * "Show: All tickers / <a watchlist>". Narrows Today and the charts to one of
 * your watchlists. Hidden until you have a watchlist.
 */
@Component({
  selector: 'app-watchlist-filter',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    @if (ctx.lists().length) {
      <label class="filter">
        <span class="label">Show</span>
        <select
          class="input"
          [attr.aria-label]="ariaLabel()"
          (change)="ctx.select($any($event.target).value)"
        >
          <option value="" [selected]="!ctx.selectedId()">All tickers</option>
          @for (w of ctx.lists(); track w.id) {
            <option [value]="w.id" [selected]="ctx.selectedId() === w.id">
              {{ w.name }} ({{ w.tickers.length }})
            </option>
          }
        </select>
      </label>
    }
  `,
  styles: `
    .filter {
      display: inline-flex;
      align-items: center;
      gap: var(--space-2);
      max-width: 100%;
    }
    .label {
      color: var(--color-ink-2);
      font-size: var(--text-sm);
      white-space: nowrap;
    }
    select {
      min-width: 0;
      max-width: 16rem;
    }
  `,
})
export class WatchlistFilter {
  protected readonly ctx = inject(WatchlistContextService);
  readonly ariaLabel = input('Filter by watchlist');

  constructor() {
    // Watchlists are personal: nothing to read for someone not signed in.
    if (inject(SessionService).signedIn()) void this.ctx.load();
  }
}
