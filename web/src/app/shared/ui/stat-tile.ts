import {
  ChangeDetectionStrategy,
  Component,
  booleanAttribute,
  computed,
  input,
} from '@angular/core';

import { splitMoney } from '../../core/format/format';
import { countUp } from './count-up';
import { HelpTip } from './help-tip';

export type StatTone = 'gain' | 'loss' | '';

/**
 * One figure in a summary strip. Format the value before passing it in (money/pct pipes).
 * Metric labels found in the glossary ("Sharpe", "Max drawdown") get a help
 * tip automatically; pass `help` to name the glossary term explicitly, or
 * `[help]="false"` to hide it.
 *
 *   <app-stat-tile label="Cash" [value]="p.cash | money" [detail]="cashShare() | pct" />
 *
 * The page's headline figure is `featured`: 52px mono, in brass when `live`
 * (real money). Pass `amount` and `format` as well and it counts up to the
 * figure.
 *
 * A value made of words (a strategy name, a date) wraps instead of running
 * off a narrow cell (UX-27) and is set in the sans, so a phrase like
 * "Too early to tell" does not shout. Figures stay on one line.
 *
 * A tile is a cell of a summary strip: put tiles in `.summary-strip` (or
 * the older `.stat-strip`), which draws the 2px ink rule on top and the 1px
 * rules between cells. `bare` is kept for older pages and changes nothing.
 *
 *   <div class="summary-strip"><app-stat-tile label="Cash" [value]="cash" /> ...</div>
 *
 *   <app-stat-tile label="Value" featured [live]="isLive()" [amount]="total" [format]="money" />
 */
@Component({
  selector: 'app-stat-tile',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [HelpTip],
  host: {
    class: 'stat-tile',
    '[class.featured]': 'featured()',
    '[class.live]': 'live()',
    '[class.text]': 'isText()',
    '[class.bare]': 'bare()',
  },
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
      <div class="figure-line">
        @if (counting()) {
          @let parts = split(shown());
          <p class="value" aria-hidden="true">
            {{ parts[0] }}<span class="minor">{{ parts[1] }}</span>
          </p>
          <span class="visually-hidden">{{ finalText() }}</span>
        } @else {
          @let parts = split(finalText());
          <p class="value">
            {{ parts[0] }}<span class="minor">{{ parts[1] }}</span>
          </p>
        }
        @if (detail()) {
          <p class="detail" [class]="detailTone()">{{ detail() }}</p>
        }
      </div>
    }
  `,
  styles: `
    @use 'breakpoints' as bp;
    /* A cell of a summary strip: no box. The strip draws the 2px ink rule
       and the 1px rules between cells. */
    :host {
      display: block;
      min-width: 0;
      container-type: inline-size;
      padding: var(--space-4) var(--space-5);
    }
    :host(:first-child) {
      padding-left: 0;
    }
    @include bp.phone {
      :host {
        padding: var(--space-3) var(--space-4);
      }
      :host(:nth-child(odd)) {
        padding-left: 0;
      }
    }
    .label {
      color: var(--muted);
      font-size: var(--text-xs);
      font-weight: var(--weight-medium);
    }
    .label app-help-tip {
      margin-left: 2px;
    }
    .figure-line {
      display: flex;
      flex-wrap: wrap;
      align-items: baseline;
      gap: 0 var(--space-2);
      margin-top: var(--space-1);
    }
    .value {
      /* Shrinks on narrow cells instead of breaking a figure across lines. */
      font-family: var(--font-mono);
      font-size: clamp(var(--text-lead), 13cqi, var(--text-stat));
      font-weight: var(--weight-semibold);
      font-variant-numeric: tabular-nums slashed-zero;
      letter-spacing: -0.03em;
      line-height: 1.25;
      white-space: nowrap;
    }
    :host(.featured) .value {
      font-size: clamp(var(--text-stat), 14cqi, var(--text-figure));
      letter-spacing: var(--tracking-figure);
      line-height: 1.08;
    }
    /* The headline figure sets its cents in the muted ink, like Today. */
    .minor {
      color: var(--muted);
      font-weight: var(--weight-medium);
    }
    .minor:empty {
      display: none;
    }
    /* Real money: the figure turns brass. */
    :host(.live) .value {
      color: var(--brass);
    }
    /* Words, not a figure: the sans, quieter. */
    :host(.text) .value {
      font-family: var(--font-sans);
      font-size: var(--text-lead);
      font-weight: var(--weight-semibold);
      letter-spacing: normal;
      white-space: normal;
      overflow-wrap: anywhere;
    }
    .detail {
      color: var(--muted);
      font-size: var(--text-sm);
      font-variant-numeric: tabular-nums;
      white-space: normal;
    }
    .detail.gain {
      color: var(--up);
    }
    .detail.loss {
      color: var(--down);
    }
    .skeleton {
      width: 60%;
      border-radius: var(--r-tag);
      background: var(--sunk);
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
  /** Inside a panel: no box, a hairline at the start (see `.stat-strip`). */
  readonly bare = input(false, { transform: booleanAttribute });
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

  /** Words, not a figure: two letters in a row ("Momentum 3fa9", "Sep 2").
   *  A leading currency code ("US$10,000", "CHF 3,400") is part of a figure,
   *  and so is anything that starts with a digit ("5 of 5", "2h ago"). */
  protected readonly isText = computed(() => {
    if (/^[+\-−]?\d/u.test((this.finalText() ?? '').trim())) return false;
    const text = (this.finalText() ?? '').replace(/^[+\-−]?\s*[A-Z]{1,3}(?:\$|\s)(?=[\d.,])/u, '');
    return /\p{L}{2}/u.test(text);
  });

  /** A featured money figure splits off its cents; anything else stays whole. */
  protected split(text: string | null): [string, string] {
    const t = text ?? '';
    const money = /[^\d\s.,+\-−%]/u.test(t) && !t.endsWith('%');
    return this.featured() && !this.isText() && money ? splitMoney(t) : [t, ''];
  }

  protected readonly helpTerm = computed(() => {
    const help = this.help();
    return help === false ? null : (help ?? this.label());
  });
}
