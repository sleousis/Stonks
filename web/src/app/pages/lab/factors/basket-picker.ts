import { ChangeDetectionStrategy, Component, input, output } from '@angular/core';

import type { UniverseView, WatchlistView } from '../../../api/models';
import type { Basket, BasketKind } from './factor-requests';

/**
 * Which names a factor is measured on: a stored universe, one of your
 * watchlists, or typed tickers. The parent owns the value.
 *
 *   <app-basket-picker idPrefix="fv" [basket]="b()" [universes]="u" [watchlists]="w"
 *     [error]="errors()['basket']" (basketChange)="b.set($event)" />
 */
@Component({
  selector: 'app-basket-picker',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    @let b = basket();
    <fieldset class="basket">
      <legend>Names</legend>
      <div class="kinds" role="radiogroup" [attr.aria-label]="'Measure on'">
        @for (k of kinds; track k.id) {
          <label class="kind">
            <input
              type="radio"
              [name]="idPrefix() + '-basket'"
              [value]="k.id"
              [checked]="b.kind === k.id"
              (change)="set({ kind: k.id })"
            />
            {{ k.label }}
          </label>
        }
      </div>
      @switch (b.kind) {
        @case ('universe') {
          <div class="field">
            <label [for]="idPrefix() + '-universe'">Universe</label>
            <select
              class="input"
              [id]="idPrefix() + '-universe'"
              [attr.aria-invalid]="!!error()"
              [attr.aria-describedby]="idPrefix() + '-basket-hint'"
              (change)="set({ universeId: $any($event.target).value })"
            >
              <option value="" [selected]="!b.universeId">Pick a universe</option>
              @for (u of universes(); track u.id) {
                <option [value]="u.id" [selected]="u.id === b.universeId">
                  {{ universeLabel(u) }}
                </option>
              }
            </select>
          </div>
        }
        @case ('watchlist') {
          <div class="field">
            <label [for]="idPrefix() + '-watchlist'">Watchlist</label>
            <select
              class="input"
              [id]="idPrefix() + '-watchlist'"
              [attr.aria-invalid]="!!error()"
              [attr.aria-describedby]="idPrefix() + '-basket-hint'"
              (change)="set({ watchlistId: $any($event.target).value })"
            >
              <option value="" [selected]="!b.watchlistId">Pick a watchlist</option>
              @for (w of watchlists(); track w.id) {
                <option [value]="w.id" [selected]="w.id === b.watchlistId">
                  {{ w.name }}, {{ w.tickers.length }} tickers
                </option>
              }
            </select>
          </div>
        }
        @case ('tickers') {
          <div class="field">
            <label [for]="idPrefix() + '-tickers'">Tickers</label>
            <textarea
              class="input"
              rows="2"
              autocomplete="off"
              spellcheck="false"
              placeholder="AAPL.US, MSFT.US, NVDA.US"
              [id]="idPrefix() + '-tickers'"
              [value]="b.tickers"
              [attr.aria-invalid]="!!error()"
              [attr.aria-describedby]="idPrefix() + '-basket-hint'"
              (input)="set({ tickers: $any($event.target).value })"
            ></textarea>
          </div>
        }
      }
      @if (error(); as e) {
        <span class="error" [id]="idPrefix() + '-basket-hint'">{{ e }}</span>
      } @else {
        <span class="hint" [id]="idPrefix() + '-basket-hint'">{{ hint() }}</span>
      }
    </fieldset>
  `,
  styles: `
    @use 'breakpoints' as bp;
    .basket {
      display: grid;
      gap: var(--space-2);
      margin: 0;
      padding: 0;
      border: 0;
      min-width: 0;
    }
    legend {
      padding: 0;
      margin-bottom: var(--space-1);
      font-size: var(--text-sm);
      font-weight: var(--weight-medium);
    }
    .kinds {
      display: flex;
      flex-wrap: wrap;
      gap: var(--space-1) var(--space-4);
    }
    .kind {
      display: inline-flex;
      align-items: center;
      gap: var(--space-2);
      min-height: 32px;
      cursor: pointer;
      @include bp.phone {
        min-height: var(--touch-min);
      }
      @include bp.coarse {
        min-height: var(--touch-min);
      }
    }
  `,
})
export class BasketPicker {
  readonly idPrefix = input.required<string>();
  readonly basket = input.required<Basket>();
  readonly universes = input<readonly UniverseView[]>([]);
  readonly watchlists = input<readonly WatchlistView[]>([]);
  readonly error = input<string | null | undefined>(null);
  readonly basketChange = output<Basket>();

  protected readonly kinds: readonly { id: BasketKind; label: string }[] = [
    { id: 'universe', label: 'Universe' },
    { id: 'watchlist', label: 'Watchlist' },
    { id: 'tickers', label: 'Tickers' },
  ];

  protected universeLabel(u: UniverseView): string {
    const name = u.name || u.id;
    return u.member_count == null ? name : `${name}, ${u.member_count} names`;
  }

  protected hint(): string {
    switch (this.basket().kind) {
      case 'universe':
        return 'Point in time: a name counts only while it was a member.';
      case 'watchlist':
        return 'Today’s tickers of the list. Past members are not included.';
      case 'tickers':
        return 'Separate with commas or spaces. Rankings need about 10 names or more.';
    }
  }

  protected set(patch: Partial<Basket>): void {
    this.basketChange.emit({ ...this.basket(), ...patch });
  }
}
