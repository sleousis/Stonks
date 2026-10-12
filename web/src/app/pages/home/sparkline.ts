import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

/** The SVG path of a line through `values`, scaled to a `w` by `h` box. */
export function linePath(values: readonly number[], w: number, h: number, pad = 2): string {
  if (values.length < 2) return '';
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  const step = w / (values.length - 1);
  return values
    .map((v, i) => {
      const x = i * step;
      const y = pad + (h - 2 * pad) * (1 - (v - min) / span);
      return `${i ? 'L' : 'M'}${x.toFixed(2)} ${y.toFixed(2)}`;
    })
    .join(' ');
}

/**
 * A small line of a strategy's recent paper value: 1.5px, green when it
 * ended up, red when it ended down, with a 10% fill under it. Decoration
 * next to the figure it summarises, so screen readers skip it.
 */
@Component({
  selector: 'app-sparkline',
  changeDetection: ChangeDetectionStrategy.OnPush,
  host: { '[attr.data-tone]': 'tone()', 'aria-hidden': 'true' },
  template: `
    @if (line(); as d) {
      <svg viewBox="0 0 120 30" preserveAspectRatio="none" focusable="false">
        <path class="fill" [attr.d]="d + ' L120 30 L0 30 Z'" />
        <path class="line" [attr.d]="d" vector-effect="non-scaling-stroke" />
      </svg>
    }
  `,
  styles: `
    :host {
      display: block;
      width: 120px;
      height: 30px;
      color: var(--up);
    }
    :host([data-tone='down']) {
      color: var(--down);
    }
    svg {
      display: block;
      width: 100%;
      height: 100%;
      overflow: visible;
    }
    .line {
      fill: none;
      stroke: currentColor;
      stroke-width: 1.5;
      stroke-linejoin: round;
    }
    .fill {
      fill: currentColor;
      opacity: 0.1;
    }
  `,
})
export class Sparkline {
  readonly values = input.required<readonly number[]>();

  protected readonly line = computed(() => linePath(this.values(), 120, 30));
  protected readonly tone = computed(() => {
    const v = this.values();
    return v.length > 1 && v.at(-1)! < v[0] ? 'down' : 'up';
  });
}
