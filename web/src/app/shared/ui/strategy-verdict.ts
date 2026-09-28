import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import type { Verdict, VerdictLevel } from '../strategy-verdict';
import { StatusPill } from './status-pill';

/** The receipt a verdict shows: a tick, a triangle or a cross next to its words. */
export const VERDICT_PILL: Readonly<Record<VerdictLevel, 'pass' | 'warn' | 'fail'>> = {
  worth: 'pass',
  promising: 'warn',
  not_yet: 'fail',
};

let nextId = 0;

/**
 * One plain verdict for a strategy (F33): the headline, the reasons in plain
 * sentences, and the quant figures folded under "Details" (projected with a
 * `details` attribute). `compact` draws the pill alone (tables).
 *
 *   <app-strategy-verdict [verdict]="v">
 *     <div details>...check rows...</div>
 *   </app-strategy-verdict>
 *   <app-strategy-verdict compact [verdict]="v" />
 */
@Component({
  selector: 'app-strategy-verdict',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [StatusPill],
  host: { '[class.compact]': 'compact()', '[attr.data-level]': 'verdict().level' },
  template: `
    @if (compact()) {
      <app-status-pill [status]="pill()" [label]="verdict().label" />
    } @else {
      <section class="verdict" [attr.aria-labelledby]="id + '-title'">
        <h2 class="verdict-head" [id]="id + '-title'">
          <span class="visually-hidden">{{ heading() }}: </span>
          <app-status-pill class="headline" [status]="pill()" [label]="verdict().label" />
        </h2>
        @if (verdict().reasons.length) {
          <ul class="reasons">
            @for (r of verdict().reasons; track $index) {
              <li>{{ r }}</li>
            }
          </ul>
        }
        @if (details()) {
          <details class="details">
            <summary>Details</summary>
            <div class="details-body"><ng-content select="[details]" /></div>
          </details>
        }
      </section>
    }
  `,
  styles: `
    :host {
      display: block;
      min-width: 0;
    }
    :host(.compact) {
      display: inline-flex;
    }
    .verdict {
      display: grid;
      gap: var(--space-2);
      padding: var(--space-4);
      border-left: 4px solid var(--color-border-strong);
    }
    :host([data-level='worth']) .verdict {
      border-left-color: var(--color-gain);
    }
    :host([data-level='promising']) .verdict {
      border-left-color: var(--color-warn);
    }
    :host([data-level='not_yet']) .verdict {
      border-left-color: var(--color-loss);
    }
    .verdict-head {
      margin: 0;
      font-size: var(--text-lg);
    }
    .headline {
      font-size: var(--text-lg);
      font-weight: var(--weight-semibold);
      line-height: 1.3;
      white-space: normal;
    }
    .reasons {
      display: grid;
      gap: var(--space-1);
      margin: 0;
      padding-left: var(--space-4);
      color: var(--color-ink-2);
    }
    .details {
      border-top: 1px solid var(--color-border);
      padding-top: var(--space-1);
    }
    .details summary {
      display: flex;
      align-items: center;
      min-height: var(--touch-min);
      cursor: pointer;
      font-weight: var(--weight-semibold);
    }
    .details-body {
      padding-top: var(--space-2);
    }
  `,
})
export class StrategyVerdict {
  readonly verdict = input.required<Verdict>();
  /** The hidden heading read before the verdict ("Verdict"). */
  readonly heading = input('Verdict');
  /** Show the "Details" fold with the projected `[details]` content. */
  readonly details = input(false, { transform: (v: boolean | '') => v !== false });
  /** The pill alone. */
  readonly compact = input(false, { transform: (v: boolean | '') => v !== false });

  protected readonly id = `verdict-${nextId++}`;
  protected readonly pill = computed(() => VERDICT_PILL[this.verdict().level]);
}
