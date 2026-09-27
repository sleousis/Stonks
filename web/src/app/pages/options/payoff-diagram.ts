import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import type { OptionPayoffView } from '../../api/models';
import { formatDate, formatMoney, formatNumber } from '../../core/format/format';
import {
  PAYOFF_BOX,
  boundText,
  costText,
  legText,
  payoffGeometry,
  structureLabel,
} from './options-view';

/**
 * A structure's profit at expiry against the underlying's price: the
 * line, gain and loss shaded either side of zero, today's price marked,
 * with its legs and the deciding figures beside it. Drawn in SVG, and
 * summed up in words for screen readers.
 */
@Component({
  selector: 'app-payoff-diagram',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    @let p = payoff();
    <div class="figures">
      <div class="figure">
        <span class="label">Cost to open</span>
        <span class="value">{{ cost() }}</span>
      </div>
      <div class="figure">
        <span class="label">Max loss</span>
        <span class="value loss">{{ maxLoss() }}</span>
      </div>
      <div class="figure">
        <span class="label">Max gain</span>
        <span class="value gain">{{ maxGain() }}</span>
      </div>
      <div class="figure">
        <span class="label">Breakeven</span>
        <span class="value">{{ breakevens() }}</span>
      </div>
    </div>

    @if (geometry(); as g) {
      <figure class="chart">
        <svg
          [attr.viewBox]="viewBox"
          role="img"
          [attr.aria-label]="summary()"
          preserveAspectRatio="xMidYMid meet"
        >
          @for (t of g.yTicks; track t.y) {
            <line
              class="grid"
              [attr.x1]="box.left"
              [attr.x2]="box.width - box.right"
              [attr.y1]="t.y"
              [attr.y2]="t.y"
            />
            <text class="tick" [attr.x]="box.left - 6" [attr.y]="t.y + 4" text-anchor="end">
              {{ t.label }}
            </text>
          }
          @for (t of g.xTicks; track t.x) {
            <text class="tick" [attr.x]="t.x" [attr.y]="box.height - 8" text-anchor="middle">
              {{ t.label }}
            </text>
          }
          <path class="area-gain" [attr.d]="g.gain" />
          <path class="area-loss" [attr.d]="g.loss" />
          <line
            class="zero"
            [attr.x1]="box.left"
            [attr.x2]="box.width - box.right"
            [attr.y1]="g.zeroY"
            [attr.y2]="g.zeroY"
          />
          @if (g.spotX !== null) {
            <line
              class="spot"
              [attr.x1]="g.spotX"
              [attr.x2]="g.spotX"
              [attr.y1]="box.top"
              [attr.y2]="box.height - box.bottom"
            />
          }
          <path class="line" [attr.d]="g.line" />
        </svg>
        <figcaption>
          Profit at expiry by the price of {{ p.underlying }}. The dashed line is today's price,
          {{ spot() }}.
        </figcaption>
      </figure>
    }

    <div class="legs">
      <h3>Legs, picked from the chain of {{ day() }}</h3>
      <ul>
        @for (leg of p.legs; track leg.instrument) {
          <li>
            <span>{{ legLine(leg) }}</span>
            <span class="price">at {{ price(leg.price) }}</span>
          </li>
        }
      </ul>
    </div>
  `,
  styles: `
    @use 'breakpoints' as bp;
    :host {
      display: grid;
      gap: var(--space-4);
      min-width: 0;
    }
    .figures {
      display: grid;
      gap: var(--space-3);
      grid-template-columns: repeat(2, minmax(0, 1fr));
      @include bp.from-tablet {
        grid-template-columns: repeat(4, minmax(0, 1fr));
      }
    }
    .figure {
      display: grid;
      gap: var(--space-1);
      min-width: 0;
    }
    .label {
      color: var(--color-ink-2);
      font-size: var(--text-sm);
    }
    .value {
      font-family: var(--font-mono);
      font-size: var(--text-lg);
      overflow-wrap: anywhere;
    }
    .gain {
      color: var(--color-gain);
    }
    .loss {
      color: var(--color-loss);
    }
    .chart {
      margin: 0;
      min-width: 0;
    }
    svg {
      display: block;
      width: 100%;
      height: auto;
    }
    .grid {
      stroke: var(--chart-grid);
      stroke-width: 1;
    }
    .tick {
      fill: var(--chart-text);
      font-size: 11px;
      font-family: var(--font-mono);
    }
    .zero {
      stroke: var(--color-ink-3);
      stroke-width: 1;
    }
    .spot {
      stroke: var(--color-ink-2);
      stroke-width: 1;
      stroke-dasharray: 4 4;
    }
    .line {
      fill: none;
      stroke: var(--chart-line);
      stroke-width: 2;
    }
    .area-gain {
      fill: var(--color-gain-soft);
    }
    .area-loss {
      fill: var(--color-loss-soft);
    }
    figcaption {
      margin-top: var(--space-2);
      color: var(--color-ink-2);
      font-size: var(--text-sm);
    }
    h3 {
      margin: 0 0 var(--space-2);
      font-size: var(--text-md);
    }
    ul {
      display: grid;
      gap: var(--space-1);
      margin: 0;
      padding: 0;
      list-style: none;
    }
    li {
      display: flex;
      flex-wrap: wrap;
      justify-content: space-between;
      gap: var(--space-2);
      font-family: var(--font-mono);
      font-size: var(--mono-scale);
    }
    .price {
      color: var(--color-ink-2);
    }
  `,
})
export class PayoffDiagram {
  readonly payoff = input.required<OptionPayoffView>();

  protected readonly box = PAYOFF_BOX;
  protected readonly viewBox = `0 0 ${PAYOFF_BOX.width} ${PAYOFF_BOX.height}`;
  protected readonly legLine = legText;

  protected readonly geometry = computed(() =>
    payoffGeometry(this.payoff().points, this.payoff().spot),
  );
  protected readonly cost = computed(() => costText(this.payoff().cost));
  protected readonly maxLoss = computed(() => boundText(this.payoff().max_loss));
  protected readonly maxGain = computed(() => boundText(this.payoff().max_gain));
  protected readonly breakevens = computed(() => {
    const b = this.payoff().breakevens;
    return b.length ? b.map((x) => formatNumber(x, { digits: 2 })).join(' and ') : 'None';
  });
  protected readonly spot = computed(() => formatNumber(this.payoff().spot, { digits: 2 }));
  protected readonly day = computed(() => formatDate(this.payoff().as_of));
  protected readonly summary = computed(() => {
    const p = this.payoff();
    return (
      `${structureLabel(p.structure)} on ${p.underlying}: ${this.cost()}, ` +
      `max loss ${this.maxLoss()}, max gain ${this.maxGain()}, ` +
      `breakeven ${this.breakevens()}.`
    );
  });

  protected price(value: number): string {
    return formatMoney(value);
  }
}
