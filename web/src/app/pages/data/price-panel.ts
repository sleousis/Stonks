import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  input,
  model,
  resource,
  signal,
} from '@angular/core';

import type { BarView } from '../../api/models';
import { MarketService } from '../../api/market.service';
import { formatMoney, formatNumber, formatPercent, toneClass } from '../../core/format/format';
import type { ChartSeries } from '../../shared/chart/chart-engine';
import { TimeSeriesChart } from '../../shared/chart/time-series-chart';
import { EmptyState, ErrorState, LoadingState } from '../../shared/ui/states';
import { parseTickers } from './ingest-request';

const INTRADAY = /^\d+(m|h)$/;

/** ISO date one year before `now`, the chart's default start. */
export function yearAgo(now: Date = new Date()): string {
  const d = new Date(now);
  d.setUTCFullYear(d.getUTCFullYear() - 1);
  return d.toISOString().slice(0, 10);
}

/** Chart point time: `YYYY-MM-DD` for daily and weekly bars, the ISO timestamp intraday. */
export function pointTime(bar: BarView, interval: string): string {
  return INTRADAY.test(interval) ? bar.timestamp : bar.timestamp.slice(0, 10);
}

/** Close (and volume) for one ticker and interval over a date range. */
@Component({
  selector: 'app-price-panel',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [TimeSeriesChart, LoadingState, EmptyState, ErrorState],
  styleUrls: ['./data-shared.scss', './price-panel.scss'],
  template: `
    <section class="panel" aria-labelledby="price-title">
      <div class="panel-head">
        <h2 id="price-title">
          Prices
          @if (ticker(); as t) {
            <span class="muted">· {{ t }} · {{ interval() }}</span>
          }
        </h2>
        @if (bars.hasValue() && bars.value().truncated) {
          <span class="muted count">Showing the latest {{ bars.value().bars.length }} bars</span>
        }
      </div>
      <form class="filters" (submit)="$event.preventDefault(); apply(tickerInput.value)">
        <div class="field">
          <label for="price-ticker">Ticker</label>
          <input
            #tickerInput
            id="price-ticker"
            class="input"
            placeholder="e.g. AAPL.US"
            autocapitalize="characters"
            autocomplete="off"
            spellcheck="false"
            [value]="ticker() ?? ''"
            (change)="apply(tickerInput.value)"
          />
        </div>
        <div class="field">
          <label for="price-interval">Interval</label>
          <select
            id="price-interval"
            class="input"
            [value]="interval()"
            (change)="interval.set($any($event.target).value)"
          >
            @for (code of intervals(); track code) {
              <option [value]="code" [selected]="code === interval()">{{ code }}</option>
            }
          </select>
        </div>
        <div class="field">
          <label for="price-start">From</label>
          <input
            id="price-start"
            class="input"
            type="date"
            [value]="start()"
            [max]="end() || null"
            (change)="start.set($any($event.target).value)"
          />
        </div>
        <div class="field">
          <label for="price-end">To</label>
          <input
            id="price-end"
            class="input"
            type="date"
            [value]="end()"
            [min]="start() || null"
            (change)="end.set($any($event.target).value)"
          />
        </div>
        <button type="submit" class="btn show">Show</button>
      </form>

      @if (!ticker()) {
        <app-empty-state
          title="Choose a ticker"
          message="Pick one from Instruments or Coverage, or type it above."
        />
      } @else if (bars.error(); as err) {
        <app-error-state title="Could not load bars" [error]="err" (retry)="bars.reload()" />
      } @else if (!bars.hasValue()) {
        <app-loading-state label="Loading bars" [rows]="6" />
      } @else if (bars.value().bars.length === 0) {
        <app-empty-state
          [title]="'No ' + interval() + ' bars for ' + ticker() + ' in this range'"
          message="Widen the dates, choose another interval, or ingest this ticker below."
        />
      } @else {
        @if (stats(); as s) {
          <dl class="stats">
            <div>
              <dt>Last close</dt>
              <dd class="num">{{ s.last }}</dd>
            </div>
            <div>
              <dt>Change</dt>
              <dd class="num" [class]="s.changeTone">{{ s.change }}</dd>
            </div>
            <div>
              <dt>High</dt>
              <dd class="num">{{ s.high }}</dd>
            </div>
            <div>
              <dt>Low</dt>
              <dd class="num">{{ s.low }}</dd>
            </div>
            <div>
              <dt>Bars</dt>
              <dd class="num">{{ s.count }}</dd>
            </div>
          </dl>
        }
        <div class="chart">
          <app-time-series-chart
            [ariaLabel]="ticker() + ' close price, with volume below'"
            [summary]="summary()"
            [series]="series()"
            [height]="340"
          />
        </div>
      }
    </section>
  `,
})
export class PricePanel {
  private readonly market = inject(MarketService);

  readonly ticker = model<string | null>(null);
  readonly interval = model('1d');
  readonly intervals = input<readonly string[]>(['1d']);
  /** Bump to refetch (after an ingest). */
  readonly refresh = input(0);

  protected readonly start = signal(yearAgo());
  protected readonly end = signal('');

  protected readonly bars = resource({
    params: () => {
      const ticker = this.ticker();
      this.refresh();
      if (!ticker) return undefined;
      return {
        ticker,
        interval: this.interval(),
        start: this.start() || undefined,
        end: this.end() || undefined,
      };
    },
    loader: ({ params }) => this.market.bars(params),
  });

  private readonly closes = computed(() => {
    if (!this.bars.hasValue()) return [];
    const interval = this.bars.value().interval;
    return this.bars
      .value()
      .bars.filter((b) => b.close != null)
      .map((b) => ({ time: pointTime(b, interval), close: b.close as number, volume: b.volume }));
  });

  protected readonly series = computed<ChartSeries[]>(() => {
    const rows = this.closes();
    const series: ChartSeries[] = [
      {
        id: 'close',
        label: 'Close',
        kind: 'line',
        color: 'primary',
        format: 'money',
        points: rows.map((r) => ({ time: r.time, value: r.close })),
      },
    ];
    if (rows.some((r) => r.volume != null && r.volume > 0)) {
      series.push({
        id: 'volume',
        label: 'Volume',
        kind: 'area',
        color: 'muted',
        pane: 1,
        format: 'number',
        points: rows.map((r) => ({ time: r.time, value: r.volume ?? 0 })),
      });
    }
    return series;
  });

  protected readonly stats = computed(() => {
    const rows = this.closes();
    const first = rows[0];
    const last = rows.at(-1);
    if (!first || !last) return null;
    const closes = rows.map((r) => r.close);
    const change = first.close ? last.close / first.close - 1 : null;
    return {
      last: formatMoney(last.close),
      change: formatPercent(change, { signed: true }),
      changeTone: toneClass(change),
      high: formatMoney(Math.max(...closes)),
      low: formatMoney(Math.min(...closes)),
      count: formatNumber(rows.length),
    };
  });

  protected readonly summary = computed(() => {
    const rows = this.closes();
    const first = rows[0];
    const last = rows.at(-1);
    const s = this.stats();
    if (!first || !last || !s) return null;
    return (
      `${this.ticker()} ${this.interval()} closes from ${first.time.slice(0, 10)} to ${last.time.slice(0, 10)}: ` +
      `${formatMoney(first.close)} to ${s.last} (${s.change}), high ${s.high}, low ${s.low}, ${s.count} bars.`
    );
  });

  protected apply(text: string): void {
    const [ticker] = parseTickers(text);
    this.ticker.set(ticker ?? null);
  }
}
