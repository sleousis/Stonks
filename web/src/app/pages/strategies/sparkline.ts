import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

/** The drawing box of a sparkline; the SVG scales to its CSS size. */
const W = 120;
const H = 30;
const PAD = 2;

export interface SparkPath {
  line: string;
  area: string;
  tone: 'up' | 'down';
}

/**
 * The line and area paths of a series of values, scaled into the 120x30 box.
 * Up when the last value ends at or above the first. Null with fewer than
 * two values.
 */
export function sparkPath(values: readonly number[]): SparkPath | null {
  const v = values.filter((x) => Number.isFinite(x));
  if (v.length < 2) return null;
  const min = Math.min(...v);
  const max = Math.max(...v);
  const span = max - min || 1;
  const step = (W - PAD * 2) / (v.length - 1);
  const points = v.map((x, i) => {
    const px = PAD + i * step;
    const py = H - PAD - ((x - min) / span) * (H - PAD * 2);
    return `${px.toFixed(1)},${py.toFixed(1)}`;
  });
  const line = `M${points.join(' L')}`;
  const area = `${line} L${(W - PAD).toFixed(1)},${H} L${PAD},${H} Z`;
  return { line, area, tone: v[v.length - 1] >= v[0] ? 'up' : 'down' };
}

/**
 * A small trend line: 1.5px in up or down by its direction, with a 10% fill.
 * Decorative next to a figure that says the same in words, so it is hidden
 * from screen readers unless given a label.
 *
 *   <app-sparkline [values]="row.curve" />
 */
@Component({
  selector: 'app-sparkline',
  changeDetection: ChangeDetectionStrategy.OnPush,
  host: { '[attr.data-tone]': 'path()?.tone ?? null' },
  template: `
    @if (path(); as p) {
      <svg
        [attr.viewBox]="'0 0 ' + w + ' ' + h"
        preserveAspectRatio="none"
        [attr.role]="label() ? 'img' : null"
        [attr.aria-label]="label()"
        [attr.aria-hidden]="label() ? null : 'true'"
      >
        <path class="area" [attr.d]="p.area" />
        <path class="line" [attr.d]="p.line" vector-effect="non-scaling-stroke" />
      </svg>
    } @else {
      <span class="none" aria-hidden="true"></span>
    }
  `,
  styles: `
    :host {
      --spark: var(--muted);
      display: inline-block;
      width: 120px;
      height: 30px;
      vertical-align: middle;
    }
    :host([data-tone='up']) {
      --spark: var(--up);
    }
    :host([data-tone='down']) {
      --spark: var(--down);
    }
    svg {
      width: 100%;
      height: 100%;
      overflow: visible;
    }
    .line {
      fill: none;
      stroke: var(--spark);
      stroke-width: 1.5;
      stroke-linejoin: round;
      stroke-linecap: round;
    }
    .area {
      fill: var(--spark);
      opacity: 0.1;
    }
    /* No series yet: a quiet dotted baseline holds the space. */
    .none {
      display: block;
      width: 100%;
      height: 50%;
      border-bottom: 1px dotted var(--rule-control);
    }
  `,
})
export class Sparkline {
  readonly values = input<readonly number[] | null>(null);
  /** A spoken summary; without one the line is decoration. */
  readonly label = input<string | null>(null);

  protected readonly w = W;
  protected readonly h = H;
  protected readonly path = computed(() => sparkPath(this.values() ?? []));
}
