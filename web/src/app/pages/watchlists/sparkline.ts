import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

const W = 120;
const H = 30;

/** The SVG paths of a sparkline: the line and the area under it. */
export function sparkPaths(values: readonly number[]): { line: string; area: string } | null {
  const vs = values.filter((v) => Number.isFinite(v));
  if (vs.length < 2) return null;
  const min = Math.min(...vs);
  const max = Math.max(...vs);
  const span = max - min || 1;
  const step = W / (vs.length - 1);
  const pts = vs.map((v, i) => {
    const x = +(i * step).toFixed(2);
    const y = +(H - 2 - ((v - min) / span) * (H - 4)).toFixed(2);
    return `${x},${y}`;
  });
  const line = `M${pts.join(' L')}`;
  return { line, area: `${line} L${W},${H} L0,${H} Z` };
}

/**
 * A 120x30 sparkline: a 1.5px line in up or down (by the first and last
 * value) over a 10% fill. Decorative: the row carries the figures, so it is
 * hidden from screen readers.
 */
@Component({
  selector: 'app-sparkline',
  changeDetection: ChangeDetectionStrategy.OnPush,
  host: { 'aria-hidden': 'true', '[attr.data-tone]': 'tone()' },
  template: `
    @if (paths(); as p) {
      <svg [attr.viewBox]="'0 0 ' + w + ' ' + h" preserveAspectRatio="none" focusable="false">
        <path class="area" [attr.d]="p.area" />
        <path class="line" [attr.d]="p.line" />
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
      width: 100%;
      height: 100%;
      overflow: visible;
    }
    .area {
      fill: currentColor;
      opacity: 0.1;
    }
    .line {
      fill: none;
      stroke: currentColor;
      stroke-width: 1.5;
      stroke-linejoin: round;
      stroke-linecap: round;
      vector-effect: non-scaling-stroke;
    }
  `,
})
export class Sparkline {
  readonly values = input.required<readonly number[]>();
  protected readonly w = W;
  protected readonly h = H;
  protected readonly paths = computed(() => sparkPaths(this.values()));
  protected readonly tone = computed(() => {
    const v = this.values();
    return v.length > 1 && v[v.length - 1] < v[0] ? 'down' : 'up';
  });
}
