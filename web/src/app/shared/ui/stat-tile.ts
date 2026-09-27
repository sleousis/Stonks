import {
  ChangeDetectionStrategy,
  Component,
  booleanAttribute,
  computed,
  input,
} from '@angular/core';

import { countUp } from './count-up';
import { HelpTip } from './help-tip';

export type StatTone = 'gain' | 'loss' | '';

/**
 * One headline figure. Format the value before passing it in (money/pct pipes).
 * Metric labels found in the glossary ("Sharpe", "Max drawdown") get a help
 * tip automatically; pass `help` to name the glossary term explicitly, or
 * `[help]="false"` to hide it.
 *
 *   <app-stat-tile label="Cash" [value]="p.cash | money" [detail]="cashShare() | pct" />
 *
 * The page's headline figure is `featured`: set in the display face, with a
 * slate rule for paper money or a brass one when `live` (real money). Pass
 * `amount` and `format` as well and it counts up to the figure.
 *
 *   <app-stat-tile label="Value" featured [live]="isLive()" [amount]="total" [format]="money" />
 */
@Component({
  selector: 'app-stat-tile',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [HelpTip],
  host: { class: 'stat-tile', '[class.featured]': 'featured()', '[class.live]': 'live()' },
  template: `
    <p class="label">
      {{ label() }}
      @if (helpTerm(); as term) {
        <app-help-tip [term]="term" />
      }
    </p>
    @if (loading()) {
      <p class="value skeleton" aria-hidden="true">&nbsp;</p>
      <span class="visually-hidden">Loading {{ label() }}</span>
    } @else {
      @if (counting()) {
        <p class="value" aria-hidden="true">{{ shown() }}</p>
        <span class="visually-hidden">{{ finalText() }}</span>
      } @else {
        <p class="value">{{ finalText() }}</p>
      }
      @if (detail()) {
        <p class="detail num" [class]="detailTone()">{{ detail() }}</p>
      }
    }
  `,
  styles: `
    :host {
      display: block;
      min-width: 0;
      container-type: inline-size;
      padding: var(--space-3) var(--space-4);
      background: var(--color-surface);
      border: 1px solid var(--color-border);
      border-radius: var(--radius-md);
    }
    :host(.featured) {
      border-left: var(--border-live) solid var(--color-paper);
    }
    :host(.featured.live) {
      border: var(--border-live) solid var(--color-live);
    }
    .label {
      font-size: var(--text-sm);
      color: var(--color-ink-2);
    }
    .label app-help-tip {
      margin-left: 2px;
    }
    .value {
      margin-top: var(--space-1);
      /* Shrinks on narrow tiles instead of breaking a figure across lines. */
      font-family: var(--font-display);
      font-stretch: var(--display-stretch);
      font-size: clamp(var(--text-lg), 15cqi, var(--text-2xl));
      font-weight: var(--weight-bold);
      font-variant-numeric: tabular-nums lining-nums;
      letter-spacing: var(--tracking-display);
      line-height: var(--leading-tight);
      white-space: nowrap;
    }
    :host(.featured) .value {
      font-size: clamp(var(--text-xl), 16cqi, var(--text-figure));
      line-height: 1;
    }
    :host(.featured.live) .value {
      color: var(--color-live);
    }
    .detail {
      margin-top: var(--space-1);
      font-size: var(--text-sm);
      color: var(--color-ink-3);
      white-space: normal;
    }
    .detail.gain {
      color: var(--color-gain);
    }
    .detail.loss {
      color: var(--color-loss);
    }
    .skeleton {
      width: 60%;
      border-radius: var(--radius-sm);
      background: var(--color-surface-3);
    }
  `,
})
export class StatTile {
  readonly label = input.required<string>();
  readonly value = input<string | null>(null);
  readonly detail = input<string | null>(null);
  readonly detailTone = input<StatTone>('');
  readonly loading = input(false);
  /** The page's headline figure: display face, larger, with a rule. One per page. */
  readonly featured = input(false, { transform: booleanAttribute });
  /** Real money: the headline figure and its frame turn brass. */
  readonly live = input(false, { transform: booleanAttribute });
  /** The raw number behind `value`; with `format`, the figure counts up to it. */
  readonly amount = input<number | null>(null);
  readonly format = input<((n: number) => string) | null>(null);
  /** Glossary key or label for the help tip; defaults to `label`, `false` hides it. */
  readonly help = input<string | false | null>(null);

  private readonly animated = countUp(() => (this.format() ? this.amount() : null));

  protected readonly counting = computed(() => this.format() !== null && this.amount() !== null);
  protected readonly finalText = computed(() => {
    const fmt = this.format();
    const amount = this.amount();
    return fmt && amount !== null ? fmt(amount) : this.value();
  });
  protected readonly shown = computed(() => {
    const fmt = this.format();
    const n = this.animated();
    return fmt && n !== null ? fmt(n) : this.finalText();
  });

  protected readonly helpTerm = computed(() => {
    const help = this.help();
    return help === false ? null : (help ?? this.label());
  });
}
