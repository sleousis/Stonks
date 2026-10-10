import { ChangeDetectionStrategy, Component, input, output } from '@angular/core';

import type { IntervalInfo } from '../../api/models';
import type { FormErrors, WindowForm } from './lab-requests';

/** The bar sizes most research needs; the rest stay in the full lab forms. */
export const COMMON_INTERVALS: readonly string[] = ['1d', '1w', '1h', '15m', '5m'];

/** Tickers, date window and bar interval: the inputs both lab forms share. */
@Component({
  selector: 'app-window-fields',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    @let p = idPrefix();
    @if (showTickers()) {
      <div class="field tickers">
        <label [for]="p + '-tickers'">{{ tickersLabel() }}</label>
        <textarea
          class="input"
          rows="2"
          autocapitalize="characters"
          autocomplete="off"
          spellcheck="false"
          placeholder="AAPL.US, MSFT.US"
          [id]="p + '-tickers'"
          [value]="value().tickers"
          [attr.aria-invalid]="!!errors()['tickers']"
          [attr.aria-describedby]="p + '-tickers-hint'"
          (input)="patch.emit({ tickers: $any($event.target).value })"
        ></textarea>
        @if (errors()['tickers']; as e) {
          <span class="error" [id]="p + '-tickers-hint'">{{ e }}</span>
        } @else {
          <span class="hint" [id]="p + '-tickers-hint'">{{ tickersHint() }}</span>
        }
      </div>
    }
    <div class="row" [class.two]="!showInterval()">
      <div class="field">
        <label [for]="p + '-start'">Start</label>
        <input
          class="input num"
          type="date"
          [id]="p + '-start'"
          [value]="value().start"
          [attr.aria-invalid]="!!errors()['start']"
          (input)="patch.emit({ start: $any($event.target).value })"
        />
        @if (errors()['start']; as e) {
          <span class="error">{{ e }}</span>
        }
      </div>
      <div class="field">
        <label [for]="p + '-end'">End</label>
        <input
          class="input num"
          type="date"
          [id]="p + '-end'"
          [value]="value().end"
          [attr.aria-invalid]="!!errors()['end']"
          [attr.aria-describedby]="errors()['end'] ? p + '-end-error' : null"
          (input)="patch.emit({ end: $any($event.target).value })"
        />
        @if (errors()['end']; as e) {
          <span class="error" [id]="p + '-end-error'">{{ e }}</span>
        }
      </div>
      @if (showInterval()) {
        <div class="field">
          <label [for]="p + '-interval'">Interval</label>
          <select
            class="input"
            [id]="p + '-interval'"
            (change)="patch.emit({ interval: $any($event.target).value })"
          >
            @for (i of intervalOptions(); track i.code) {
              <option [value]="i.code" [selected]="i.code === value().interval">
                {{ i.code }}{{ i.is_intraday ? ' (intraday)' : '' }}
              </option>
            }
          </select>
        </div>
      }
    </div>
  `,
  styles: `
    @use 'breakpoints' as bp;
    :host {
      display: grid;
      gap: var(--space-4);
    }
    textarea.input {
      padding: var(--space-2) var(--space-3);
      min-height: 4rem;
      resize: vertical;
      line-height: var(--leading-body);
      text-transform: uppercase;
    }
    textarea.input::placeholder {
      text-transform: none;
    }
    .row {
      display: grid;
      gap: var(--space-4);
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }
    .row > :last-child {
      grid-column: 1 / -1;
    }
    @include bp.from-tablet {
      .row {
        grid-template-columns: repeat(3, minmax(0, 1fr));
      }
      .row > :last-child {
        grid-column: auto;
      }
    }
    // Start and End only (no interval): two equal columns everywhere.
    .row.two {
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }
    .row.two > :last-child {
      grid-column: auto;
    }
    .field {
      align-content: start;
      min-width: 0;
    }
    input[type='date'] {
      min-width: 0;
    }
  `,
})
export class WindowFields {
  readonly value = input.required<WindowForm>();
  readonly errors = input<FormErrors>({});
  readonly intervals = input<readonly IntervalInfo[]>([]);
  readonly idPrefix = input('window');
  /** Hidden when a stored universe replaces the typed tickers. */
  readonly showTickers = input(true);
  /** Off in the simple test, which always uses daily bars. */
  readonly showInterval = input(true);
  /** Offer only the common bar sizes (Sweep, Signal check). */
  readonly commonIntervals = input(false);
  /** The tickers field's label: "Tickers to test" on every lab form. */
  readonly tickersLabel = input('Tickers to test');
  readonly tickersHint = input(
    'Separate with commas, spaces or new lines. Each needs price history in our data.',
  );
  readonly patch = output<Partial<WindowForm>>();

  /** Falls back to daily bars while the catalog loads (or if it failed). */
  protected intervalOptions(): readonly IntervalInfo[] {
    const all = this.intervals();
    const list = this.commonIntervals()
      ? all.filter((i) => COMMON_INTERVALS.includes(i.code) || i.code === this.value().interval)
      : all;
    return list.length ? list : [{ code: '1d', is_intraday: false, seconds: 86_400 }];
  }
}
