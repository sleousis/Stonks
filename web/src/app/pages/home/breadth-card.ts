import { ChangeDetectionStrategy, Component, computed, inject, resource } from '@angular/core';

import { MarketService } from '../../api/market.service';
import type { BreadthView } from '../../api/models';
import { formatDate, formatNumber, formatPercent } from '../../core/format/format';
import { ErrorState, LoadingState } from '../../shared/ui/states';

type Breadth = BreadthView['breadth'];

/** "60% (300 of 500)", or why there is no share yet (screen readers, exports). */
export function aboveText(a: Breadth['above_50']): string {
  if (a.pct === null || a.pct === undefined) return 'Not enough history';
  return `${formatPercent(a.pct, { digits: 0 })} (${formatNumber(a.count)} of ${formatNumber(a.eligible)})`;
}

/**
 * How many stocks take part in the market's move (roadmap 23.14): rises and
 * falls, the share above the 50 and 200 day averages, new one year highs and
 * lows, and distribution days on the index: a split bar of rises against
 * falls, four figures, and a callout reading them in plain sentences.
 * Display only: nothing here trades.
 */
@Component({
  selector: 'app-breadth-card',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [LoadingState, ErrorState],
  template: `
    <section class="page-section" aria-labelledby="home-breadth">
      <header class="section-head">
        <h2 id="home-breadth">Market breadth</h2>
        @if (asOf(); as day) {
          <span class="as-of num">{{ day }}</span>
        }
      </header>
      @if (breadth.error(); as err) {
        <div class="section-body">
          <app-error-state
            title="Could not load breadth"
            [error]="err"
            (retry)="breadth.reload()"
          />
        </div>
      } @else if (!breadth.hasValue()) {
        <div class="section-body">
          <app-loading-state label="Loading market breadth" [rows]="3" />
        </div>
      } @else if (b(); as v) {
        <div class="section-body body">
          @if (v.as_of) {
            <div class="split" role="img" [attr.aria-label]="upDown()">
              <span class="bar up" [style.flex-grow]="v.advancers || 0.0001"></span>
              <span class="bar down" [style.flex-grow]="v.decliners || 0.0001"></span>
            </div>
            <p class="split-labels num" aria-hidden="true">
              <span class="gain">{{ upWord() }}</span>
              @if (ratio(); as r) {
                <span class="ratio">{{ r }}</span>
              }
              <span class="loss">{{ downWord() }}</span>
            </p>
            <p class="visually-hidden">{{ upDown() }}. {{ ratio() }}</p>
            <dl class="figures">
              <div>
                <dt>Above 50 day average</dt>
                <dd>
                  <span class="stat-figure">{{ pct(v.above_50) }}</span>
                  <span class="of">{{ ofText(v.above_50) }}</span>
                </dd>
              </div>
              <div>
                <dt>Above 200 day average</dt>
                <dd>
                  <span class="stat-figure">{{ pct(v.above_200) }}</span>
                  <span class="of">{{ ofText(v.above_200) }}</span>
                </dd>
              </div>
              <div>
                <dt>New highs and lows</dt>
                <dd>
                  <span class="visually-hidden">{{ highsLows() }}</span>
                  <span aria-hidden="true">
                    <span class="stat-figure">{{ v.new_highs }}</span>
                    <span class="of">highs</span>
                    <span class="stat-figure">{{ v.new_lows }}</span>
                    <span class="of">lows</span>
                  </span>
                </dd>
              </div>
              @if (v.distribution_days !== null && v.distribution_days !== undefined) {
                <div>
                  <dt>Distribution days</dt>
                  <dd>
                    <span class="stat-figure">{{ v.distribution_days }}</span>
                    <span class="of">in {{ v.distribution_window }} sessions</span>
                    <span class="meaning">{{ distributionMeaning() }}</span>
                  </dd>
                </div>
              }
            </dl>
          }
          <div class="callout reading">
            <ul class="lines" aria-label="What the numbers mean">
              @for (line of v.lines; track line.key) {
                <li [class]="'line ' + line.tone">{{ line.text }}</li>
              }
            </ul>
            <p class="hint">
              Measured over {{ universe() }}. For context only, nothing trades on it.
            </p>
          </div>
        </div>
      }
    </section>
  `,
  styles: `
    :host {
      display: block;
      min-width: 0;
    }
    .as-of {
      color: var(--muted);
      font-size: var(--text-xs);
    }
    .body {
      display: grid;
      gap: var(--space-4);
    }
    /* Rises against falls: an 8px split bar with a 4px gap. */
    .split {
      display: flex;
      gap: 4px;
      height: 8px;
    }
    .bar {
      flex-basis: 0;
      min-width: 2px;
      border-radius: 2px;
    }
    .bar.up {
      background: var(--up);
    }
    .bar.down {
      background: var(--down);
    }
    .split-labels {
      display: flex;
      justify-content: space-between;
      margin-top: calc(var(--space-2) * -1);
      font-size: var(--text-xs);
    }
    .ratio {
      color: var(--muted);
      text-align: center;
    }
    /* The four figures: a 2x2 grid on hairlines. */
    .figures {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 0 var(--space-5);
      margin: 0;
    }
    .figures > div {
      padding: var(--space-3) 0;
      border-top: 1px solid var(--rule);
    }
    dt {
      color: var(--muted);
      font-size: var(--text-xs);
    }
    dd {
      margin: 2px 0 0;
    }
    .of {
      margin: 0 var(--space-2) 0 var(--space-1);
      color: var(--muted);
      font-family: var(--font-mono);
      font-size: var(--text-xs);
    }
    .meaning {
      display: block;
      color: var(--muted);
      font-size: var(--text-xs);
    }
    .lines {
      display: grid;
      gap: var(--space-1);
      margin: 0;
      padding: 0;
      list-style: none;
      color: var(--ink);
      font-size: var(--text-md);
    }
    .hint {
      color: var(--muted);
      font-size: var(--text-sm);
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
    // The lake is our word for the data store: say what it means (FR-18).
    this.breadth.hasValue()
      ? this.breadth.value().universe.replace(/\bin the lake\b/i, 'Stonks tracks')
      : '',
  );
  /** What a distribution day is, in place of a bare term (FR-18). */
  protected readonly distributionMeaning = computed(() => {
    const index = this.indexName();
    return index ? `Down days on heavy volume on ${index}` : 'Down days on heavy volume';
  });
  protected readonly asOf = computed(() => {
    const day = this.b()?.as_of;
    return day ? `Close of ${formatDate(day)}` : null;
  });
  /** "60%", or a dash when no stock has the average yet. */
  protected pct(a: Breadth['above_50']): string {
    return a.pct == null ? 'n/a' : formatPercent(a.pct, { digits: 0 });
  }
  /** "300 of 500", or why there is no share yet. */
  protected ofText(a: Breadth['above_50']): string {
    return a.pct == null
      ? 'Not enough history'
      : `${formatNumber(a.count)} of ${formatNumber(a.eligible)}`;
  }
  protected readonly upWord = computed(() => `${formatNumber(this.b()?.advancers ?? 0)} up`);
  protected readonly downWord = computed(() => `${formatNumber(this.b()?.decliners ?? 0)} down`);
  protected readonly upDown = computed(() => {
    const v = this.b();
    return v ? `${formatNumber(v.advancers)} up, ${formatNumber(v.decliners)} down` : '';
  });
  protected readonly ratio = computed(() => {
    const r = this.b()?.advance_decline_ratio;
    return r === null || r === undefined ? null : `${formatNumber(r, { digits: 2 })} up per down`;
  });
  protected readonly highsLows = computed(() => {
    const v = this.b();
    return v ? `${formatNumber(v.new_highs)} highs, ${formatNumber(v.new_lows)} lows` : '';
  });
  protected readonly indexName = computed(() => (this.b()?.index ?? '').split('.')[0]);
}
