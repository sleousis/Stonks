import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import type { BacktestResult } from '../../api/models';
import { formatMoney, formatNumber, formatPercent, toneClass } from '../../core/format/format';
import type { ChartSeries } from '../../shared/chart/chart-engine';
import { TimeSeriesChart } from '../../shared/chart/time-series-chart';
import { StatTile } from '../../shared/ui/stat-tile';
import {
  FigureGrid,
  benchmarkFigures,
  benchmarkName,
  numOrNa,
  pctOrNa,
  rebase,
  riskFigures,
  tradeFigures,
} from './result-figures';

/** Drawdown from the running peak, as a fraction (0 at a new high, negative below). */
export function drawdownSeries(values: readonly number[]): number[] {
  let peak = -Infinity;
  return values.map((v) => {
    peak = Math.max(peak, v);
    return peak > 0 ? v / peak - 1 : 0;
  });
}

/** Chart time: the date for daily bars, the full timestamp for intraday bars. */
export function chartTime(timestamp: string, interval: string): string {
  const intraday = /^\d+(m|h)$/.test(interval);
  return intraday ? timestamp : timestamp.slice(0, 10);
}

/**
 * One backtest: headline tiles, the equity curve (against its benchmark,
 * both rebased to 100, when there is one) with drawdown below, then risk,
 * trade and benchmark figures. Non-finite figures read "n/a".
 */
@Component({
  selector: 'app-backtest-result',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [StatTile, TimeSeriesChart, FigureGrid],
  template: `
    @let r = result();
    <p class="meta muted">
      <span class="num">{{ r.strategy_id }}</span> · {{ r.interval }} bars ·
      <span class="num">{{ r.start }} to {{ r.end }}</span>
    </p>
    <div class="tiles" role="group" aria-label="Backtest metrics">
      <app-stat-tile
        class="tile-return"
        label="Total return"
        featured
        [value]="pct(r.final_return, true)"
        [detail]="endValue()"
        [detailTone]="tone(r.final_return)"
      />
      <app-stat-tile label="CAGR" [value]="pct(r.cagr, true)" />
      <app-stat-tile label="Sharpe" [value]="num(r.sharpe, 2)" />
      <app-stat-tile label="Max drawdown" [value]="pct(r.max_drawdown)" />
      <app-stat-tile label="Profit factor" [value]="num(r.profit_factor, 2)" />
      @if (trades() !== null) {
        <app-stat-tile label="Trades" [value]="num(trades(), 0)" />
      }
    </div>

    @if (r.equity.length > 1) {
      <app-time-series-chart
        [ariaLabel]="
          hasBenchmark()
            ? 'Equity against the benchmark, both rebased to 100, with drawdown below'
            : 'Backtest equity, with drawdown below'
        "
        [summary]="summary()"
        [series]="series()"
        [height]="300"
      />
      @if (hasBenchmark()) {
        <p class="legend muted">
          <span class="swatch equity" aria-hidden="true"></span> Strategy
          <span class="swatch bench" aria-hidden="true"></span> {{ benchLabel() }}, both start at
          100.
        </p>
      }
    } @else {
      <p class="muted none">No equity curve: the window had fewer than two bars.</p>
    }

    <section aria-labelledby="bt-risk-title">
      <h3 id="bt-risk-title">Risk</h3>
      <app-figure-grid label="Risk figures" [figures]="risk()" />
    </section>

    <section aria-labelledby="bt-trades-title">
      <h3 id="bt-trades-title">Trades</h3>
      @if (r.trade_stats) {
        <app-figure-grid label="Trade figures" [figures]="tradeStats()" />
      } @else {
        <p class="muted none">No trade statistics for this result.</p>
      }
    </section>

    <section aria-labelledby="bt-bench-title">
      <h3 id="bt-bench-title">Against the benchmark</h3>
      @if (r.benchmark; as b) {
        <p class="muted bench-name">
          {{ benchLabel() }} · <span class="num">{{ b.n_obs }}</span> bars compared
        </p>
        <app-figure-grid label="Benchmark figures" [figures]="bench()" />
      } @else {
        <p class="muted none">
          No benchmark for this run. Pick one (auto, EW or a ticker) in the form to compare.
        </p>
      }
    </section>
  `,
  styles: `
    @use 'breakpoints' as bp;
    :host {
      display: grid;
      gap: var(--space-4);
      min-width: 0;
    }
    .meta {
      font-size: var(--text-sm);
      overflow-wrap: anywhere;
    }
    .tiles {
      display: grid;
      gap: var(--space-3);
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }
    .tile-return {
      grid-column: 1 / -1;
    }
    @include bp.from-tablet {
      .tiles {
        grid-template-columns: repeat(4, minmax(0, 1fr));
      }
    }
    h3 {
      font-size: var(--text-md);
      margin-bottom: var(--space-2);
    }
    .none,
    .bench-name,
    .legend {
      font-size: var(--text-sm);
    }
    .bench-name {
      margin-bottom: var(--space-2);
      overflow-wrap: anywhere;
    }
    .legend {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: var(--space-1) var(--space-2);
      margin-top: calc(-1 * var(--space-2));
    }
    .swatch {
      display: inline-block;
      width: 14px;
      height: 3px;
      border-radius: 2px;
    }
    .swatch.equity {
      background: var(--color-brass);
    }
    .swatch.bench {
      background: var(--color-ink-3);
    }
  `,
})
export class BacktestResultView {
  readonly result = input.required<BacktestResult>();

  /** The API's trade count (older results carry none). */
  protected readonly trades = computed(() => {
    const r = this.result();
    if (typeof r.trade_count === 'number') return r.trade_count;
    return r.trade_stats ? r.trade_stats.n_trades : null;
  });

  private readonly values = computed(() => this.result().equity.map((p) => p.value));
  /** The API's drawdown series when it sends one, else computed from equity. */
  private readonly drawdowns = computed(() => {
    const r = this.result();
    return r.drawdown?.length === r.equity.length
      ? r.drawdown.map((p) => p.value)
      : drawdownSeries(this.values());
  });

  protected readonly hasBenchmark = computed(
    () => (this.result().benchmark_equity?.length ?? 0) > 1,
  );
  protected readonly benchLabel = computed(() => {
    const b = this.result().benchmark;
    return b ? benchmarkName(b) : 'Benchmark';
  });

  protected readonly risk = computed(() => riskFigures(this.result()));
  protected readonly tradeStats = computed(() => {
    const t = this.result().trade_stats;
    return t ? tradeFigures(t) : [];
  });
  protected readonly bench = computed(() => {
    const b = this.result().benchmark;
    return b ? benchmarkFigures(b) : [];
  });

  protected readonly endValue = computed(() => {
    const v = this.values();
    return v.length ? `${formatMoney(v[0])} to ${formatMoney(v.at(-1))}` : null;
  });

  protected readonly series = computed<ChartSeries[]>(() => {
    const r = this.result();
    const dd = this.drawdowns();
    const times = r.equity.map((p) => chartTime(p.timestamp, r.interval));
    const drawdown: ChartSeries = {
      id: 'drawdown',
      label: 'Drawdown',
      kind: 'area',
      color: 'loss',
      pane: 1,
      format: 'percent',
      points: dd.map((value, i) => ({ time: times[i], value })),
    };
    if (!this.hasBenchmark()) {
      return [
        {
          id: 'equity',
          label: 'Equity',
          kind: 'line',
          color: 'brass',
          format: 'money',
          points: r.equity.map((p, i) => ({ time: times[i], value: p.value })),
        },
        drawdown,
      ];
    }
    const bench = r.benchmark_equity ?? [];
    const strategy = rebase(r.equity);
    const benchmark = rebase(bench);
    return [
      {
        id: 'equity',
        label: 'Strategy',
        kind: 'line',
        color: 'brass',
        format: 'number',
        points: strategy.map((value, i) => ({ time: times[i], value })),
      },
      {
        id: 'benchmark',
        label: this.benchLabel(),
        kind: 'line',
        color: 'muted',
        format: 'number',
        points: bench.map((p, i) => ({
          time: chartTime(p.timestamp, r.interval),
          value: benchmark[i],
        })),
      },
      drawdown,
    ];
  });

  protected readonly summary = computed(() => {
    const r = this.result();
    const worst = Math.min(0, ...this.drawdowns());
    let text =
      `Equity from ${r.start} to ${r.end}: ${this.endValue()}, total return ` +
      `${formatPercent(r.final_return, { signed: true })}. Worst drawdown ${formatPercent(worst)}.`;
    if (this.hasBenchmark()) {
      const bench = rebase(r.benchmark_equity ?? []).at(-1);
      const mine = rebase(r.equity).at(-1);
      text +=
        ` Rebased to 100, the strategy ends at ${formatNumber(mine, { digits: 1 })} and ` +
        `${this.benchLabel()} at ${formatNumber(bench, { digits: 1 })}.`;
    }
    return text;
  });

  protected pct(v: number | null | undefined, signed = false): string {
    return pctOrNa(v, signed);
  }

  protected num(v: number | null | undefined, digits: number): string {
    return numOrNa(v, digits);
  }

  protected tone(v: number | null) {
    return toneClass(v);
  }
}
