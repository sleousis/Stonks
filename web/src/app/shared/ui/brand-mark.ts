import { ChangeDetectionStrategy, Component, input } from '@angular/core';

export type BrandMarkMode = 'still' | 'loading' | 'broken';

/**
 * The favicon's rising line, the console's one brand mark. It is the
 * loading indicator (the line draws itself), the empty-state mark (still)
 * and, dropping at the end, the error mark (broken). Decorative: the state
 * around it carries the words.
 *
 *   <app-brand-mark mode="loading" />
 */
@Component({
  selector: 'app-brand-mark',
  changeDetection: ChangeDetectionStrategy.OnPush,
  host: { '[attr.data-mode]': 'mode()', 'aria-hidden': 'true' },
  template: `
    <svg viewBox="0 0 32 32" [attr.width]="size()" [attr.height]="size()" focusable="false">
      <path class="base" [attr.d]="path(mode())" pathLength="100" />
      <path class="line" [attr.d]="path(mode())" pathLength="100" />
    </svg>
  `,
  styles: `
    :host {
      display: inline-flex;
      flex: none;
      color: var(--color-accent);
    }
    :host([data-mode='broken']) {
      color: var(--color-loss);
    }
    svg {
      overflow: visible;
    }
    path {
      fill: none;
      stroke: currentColor;
      stroke-width: 3;
      stroke-linecap: round;
      stroke-linejoin: round;
    }
    .base {
      opacity: 0.18;
    }
    :host([data-mode='still']) .base,
    :host([data-mode='broken']) .base {
      display: none;
    }
    :host([data-mode='loading']) .line {
      stroke-dasharray: 100;
      animation: draw 1.6s var(--ease-out) infinite;
    }
    @keyframes draw {
      0% {
        stroke-dashoffset: 100;
      }
      55%,
      80% {
        stroke-dashoffset: 0;
        opacity: 1;
      }
      100% {
        stroke-dashoffset: 0;
        opacity: 0;
      }
    }
    @media (prefers-reduced-motion: reduce) {
      :host([data-mode='loading']) .line {
        animation: none;
        stroke-dasharray: none;
        opacity: 0.6;
      }
    }
  `,
})
export class BrandMark {
  readonly mode = input<BrandMarkMode>('still');
  readonly size = input(28);

  protected path(mode: BrandMarkMode): string {
    // Same strokes as public/favicon.svg; the broken mark turns down at the end.
    return mode === 'broken' ? 'M6 22l6-6 5 4 4-4 5 9' : 'M6 22l6-6 5 4 9-10';
  }
}
