import { ChangeDetectionStrategy, Component, computed, inject, resource } from '@angular/core';

import { MarketService } from '../../api/market.service';
import type { BreadthView } from '../../api/models';
import { formatDate, formatNumber, formatPercent } from '../../core/format/format';
import { StatTile } from '../../shared/ui/stat-tile';
import { ErrorState, LoadingState } from '../../shared/ui/states';

type Breadth = BreadthView['breadth'];

/** "2 of 3" with the share, or a dash when no stock has the average yet. */
export function aboveText(a: Breadth['above_50']): string {
  if (a.pct === null || a.pct === undefined) return 'Not enough history';
  return `${formatPercent(a.pct, { digits: 0 })} (${formatNumber(a.count)} of ${formatNumber(a.eligible)})`;
}

/**
 * How many stocks take part in the market's move (roadmap 23.14): rises and
 * falls, the share above the 50 and 200 day averages, new one year highs and
 * lows, and distribution days on the index. Each number comes with a plain
 * sentence. Display only: nothing here trades.
 */
@Component({
  selector: 'app-breadth-card',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [StatTile, LoadingState, ErrorState],
  template: `
    <section class="panel" aria-labelledby="home-breadth">
      <div class="panel-head">
        <h2 id="home-breadth">Market breadth</h2>
        @if (asOf(); as day) {
          <span class="hint">{{ day }}</span>
        }
      </div>
      @if (breadth.error(); as err) {
        <app-error-state title="Could not load breadth" [error]="err" (retry)="breadth.reload()" />
      } @else if (!breadth.hasValue()) {
        <app-loading-state label="Loading market breadth" [rows]="3" />
      } @else if (b(); as v) {
        <div class="panel-body body">
          @if (v.as_of) {
            <div class="tiles">
              <app-stat-tile
                label="Up and down"
                [value]="upDown()"
                [detail]="ratio()"
                [help]="false"
              />
              <app-stat-tile label="Above 50 day average" [value]="above50()" [help]="false" />
              <app-stat-tile label="Above 200 day average" [value]="above200()" [help]="false" />
              <app-stat-tile label="New highs and lows" [value]="highsLows()" [help]="false" />
              @if (v.distribution_days !== null && v.distribution_days !== undefined) {
                <app-stat-tile
                  [label]="'Distribution days on ' + indexName()"
                  [value]="distribution()"
                  [help]="false"
                />
              }
            </div>
          }
          <ul class="lines" aria-label="What the numbers mean">
            @for (line of v.lines; track line.key) {
              <li [class]="'line ' + line.tone">
                <span class="dot" aria-hidden="true"></span>
                <span>{{ line.text }}</span>
              </li>
            }
          </ul>
          <p class="hint">
            Measured over {{ universe() }}. For context only, nothing trades on it.
          </p>
        </div>
      }
    </section>
  `,
  styles: `
    :host {
      display: block;
      min-width: 0;
    }
    .body {
      display: grid;
      gap: var(--space-3);
    }
    .tiles {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(10rem, 1fr));
      gap: var(--space-3);
    }
    .lines {
      list-style: none;
      margin: 0;
      padding: 0;
      display: grid;
      gap: var(--space-2);
      font-size: var(--text-sm);
    }
    .line {
      display: flex;
      align-items: baseline;
      gap: var(--space-2);
    }
    .dot {
      flex: none;
      width: 0.5rem;
      height: 0.5rem;
      border-radius: 50%;
      background: var(--color-ink-3);
    }
    .good .dot {
      background: var(--color-gain);
    }
    .bad .dot {
      background: var(--color-loss);
    }
    .hint {
      font-size: var(--text-xs);
      color: var(--color-ink-3);
    }
  `,
})
export class BreadthCard {
  private readonly api = inject(MarketService);
  protected readonly breadth = resource({ loader: () => this.api.breadth() });

  protected readonly b = computed(() =>
    this.breadth.hasValue() ? this.breadth.value().breadth : null,
  );
  protected readonly universe = computed(() =>
    this.breadth.hasValue() ? this.breadth.value().universe : '',
  );
  protected readonly asOf = computed(() => {
    const day = this.b()?.as_of;
    return day ? `Close of ${formatDate(day)}` : null;
  });
  protected readonly upDown = computed(() => {
    const v = this.b();
    return v ? `${formatNumber(v.advancers)} up, ${formatNumber(v.decliners)} down` : '';
  });
  protected readonly ratio = computed(() => {
    const r = this.b()?.advance_decline_ratio;
    return r === null || r === undefined ? null : `${formatNumber(r, { digits: 2 })} up per down`;
  });
  protected readonly above50 = computed(() => {
    const v = this.b();
    return v ? aboveText(v.above_50) : '';
  });
  protected readonly above200 = computed(() => {
    const v = this.b();
    return v ? aboveText(v.above_200) : '';
  });
  protected readonly highsLows = computed(() => {
    const v = this.b();
    return v ? `${formatNumber(v.new_highs)} highs, ${formatNumber(v.new_lows)} lows` : '';
  });
  protected readonly indexName = computed(() => (this.b()?.index ?? '').split('.')[0]);
  protected readonly distribution = computed(() => {
    const v = this.b();
    return v
      ? `${formatNumber(v.distribution_days ?? 0)} in ${v.distribution_window} sessions`
      : '';
  });
}
