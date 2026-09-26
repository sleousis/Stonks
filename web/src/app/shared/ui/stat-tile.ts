import { ChangeDetectionStrategy, Component, booleanAttribute, input } from '@angular/core';

export type StatTone = 'gain' | 'loss' | '';

/**
 * One headline figure. Format the value before passing it in (money/pct pipes).
 *
 *   <app-stat-tile label="Cash" [value]="p.cash | money" [detail]="cashShare() | pct" />
 */
@Component({
  selector: 'app-stat-tile',
  changeDetection: ChangeDetectionStrategy.OnPush,
  host: { class: 'stat-tile', '[class.featured]': 'featured()' },
  template: `
    <p class="label">{{ label() }}</p>
    @if (loading()) {
      <p class="value skeleton" aria-hidden="true">&nbsp;</p>
      <span class="visually-hidden">Loading {{ label() }}</span>
    } @else {
      <p class="value num">{{ value() }}</p>
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
      border-left: 3px solid var(--color-brass);
    }
    .label {
      font-size: var(--text-sm);
      color: var(--color-ink-2);
    }
    .value {
      margin-top: var(--space-1);
      /* Shrinks on narrow tiles instead of breaking a figure across lines. */
      font-size: clamp(var(--text-md), 14cqi, var(--text-xl));
      font-weight: var(--weight-semibold);
      letter-spacing: -0.01em;
    }
    :host(.featured) .value {
      font-size: clamp(var(--text-lg), 12cqi, var(--text-2xl));
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
  /** The page's headline figure: larger, brass rule. One per page. */
  readonly featured = input(false, { transform: booleanAttribute });
}
