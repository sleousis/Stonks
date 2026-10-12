import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import { linePath } from './sparkline';

export interface EquityPoint {
  day: string;
  value: number;
}

const W = 1000;
const H = 220;
const PAD = 12;
const TICKS = 5;

/** "11 Sep" in the reader's locale, for the axis under the curve. */
export function axisDay(day: string): string {
  const d = new Date(`${day}T12:00:00Z`);
  return Number.isNaN(d.getTime())
    ? day
    : new Intl.DateTimeFormat(undefined, { day: 'numeric', month: 'short', timeZone: 'UTC' })
        .format(d)
        .replace('.', '');
}

/** Up to `count` evenly spaced indexes across `n` points, first and last included. */
export function tickIndexes(n: number, count = TICKS): number[] {
  if (n <= 0) return [];
  if (n <= count) return [...Array(n).keys()];
  return [...Array(count).keys()].map((i) => Math.round((i * (n - 1)) / (count - 1)));
}

/**
 * The portfolio's value as an area: a 1.75px line in the accent (brass for
 * a live portfolio) over a soft fill, dotted gridlines, a dot on the last
 * day and mono dates under it. No y axis line. The figure above already says
 * the value, and `summary` says the move for screen readers.
 */
@Component({
  selector: 'app-equity-area',
  changeDetection: ChangeDetectionStrategy.OnPush,
  host: { '[class.live]': 'live()' },
  template: `
    <figure>
      <div class="plot" role="img" [attr.aria-label]="summary()">
        <svg [attr.viewBox]="'0 0 ' + w + ' ' + h" preserveAspectRatio="none" focusable="false">
          @for (y of grid; track y) {
            <line class="grid" x1="0" [attr.y1]="y" [attr.x2]="w" [attr.y2]="y" />
          }
          @if (path(); as d) {
            <path class="fill" [attr.d]="d + ' L' + w + ' ' + h + ' L0 ' + h + ' Z'" />
            <path class="line" [attr.d]="d" vector-effect="non-scaling-stroke" />
          }
        </svg>
        @if (lastY() !== null) {
          <span class="dot" [style.top.%]="lastY()" aria-hidden="true"></span>
        }
      </div>
      <figcaption class="axis num" aria-hidden="true">
        @for (t of ticks(); track t.key) {
          <span [style.left.%]="t.left" [attr.data-edge]="t.edge">{{ t.label }}</span>
        }
      </figcaption>
    </figure>
  `,
  styles: `
    :host {
      display: block;
      min-width: 0;
      --curve: var(--accent);
      --curve-fill: var(--chart-fill);
    }
    :host(.live) {
      --curve: var(--brass-fill);
      --curve-fill: color-mix(in srgb, var(--brass-fill) 16%, transparent);
    }
    figure {
      margin: 0;
    }
    .plot {
      position: relative;
      height: 220px;
    }
    svg {
      display: block;
      width: 100%;
      height: 100%;
      overflow: visible;
    }
    .grid {
      stroke: var(--rule);
      stroke-width: 1;
      stroke-dasharray: 2 4;
      vector-effect: non-scaling-stroke;
    }
    .fill {
      fill: var(--curve-fill);
    }
    .line {
      fill: none;
      stroke: var(--curve);
      stroke-width: 1.75;
      stroke-linejoin: round;
    }
    .dot {
      position: absolute;
      right: -4px;
      width: 8px;
      height: 8px;
      margin-top: -4px;
      border-radius: 50%;
      background: var(--curve);
      box-shadow: 0 0 0 2px var(--bg);
    }
    .axis {
      position: relative;
      height: 16px;
      margin-top: var(--space-2);
      color: var(--muted);
      font-size: var(--text-2xs);
    }
    .axis span {
      position: absolute;
      top: 0;
      transform: translateX(-50%);
      white-space: nowrap;
    }
    .axis span[data-edge='start'] {
      transform: none;
    }
    .axis span[data-edge='end'] {
      transform: translateX(-100%);
    }
    @media (max-width: 767.98px) {
      .plot {
        height: 160px;
      }
      .axis span:nth-child(even) {
        display: none;
      }
    }
  `,
})
export class EquityArea {
  readonly points = input.required<readonly EquityPoint[]>();
  readonly live = input(false);
  /** What the curve shows, in one sentence, for screen readers. */
  readonly summary = input.required<string>();

  protected readonly w = W;
  protected readonly h = H;
  protected readonly grid = [PAD, H / 2, H - PAD];

  private readonly values = computed(() => this.points().map((p) => p.value));
  protected readonly path = computed(() => linePath(this.values(), W, H, PAD));

  /** Where the last point sits, as a share of the height. */
  protected readonly lastY = computed(() => {
    const v = this.values();
    if (v.length < 2) return null;
    const min = Math.min(...v);
    const span = Math.max(...v) - min || 1;
    const y = PAD + (H - 2 * PAD) * (1 - (v.at(-1)! - min) / span);
    return (y / H) * 100;
  });

  protected readonly ticks = computed(() => {
    const pts = this.points();
    const idx = tickIndexes(pts.length);
    return idx.map((i, n) => ({
      key: pts[i].day,
      label: axisDay(pts[i].day),
      left: pts.length > 1 ? (i / (pts.length - 1)) * 100 : 0,
      edge: n === 0 ? 'start' : n === idx.length - 1 ? 'end' : null,
    }));
  });
}
