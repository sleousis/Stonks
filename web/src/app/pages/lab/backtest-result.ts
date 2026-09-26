import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import type { BacktestResult } from '../../api/models';
import { formatMoney, formatNumber, formatPercent, toneClass } from '../../core/format/format';
import type { ChartSeries } from '../../shared/chart/chart-engine';
import { TimeSeriesChart } from '../../shared/chart/time-series-chart';
import { StatTile } from '../../shared/ui/stat-tile';

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
 * Metrics and equity/drawdown chart of one backtest. The API has no trade
 * count yet; a `trades` field is shown when a future contract adds it.
 */
@Component({
  selector: 'app-backtest-result',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [StatTile, TimeSeriesChart],
  template: `
    <p class="meta muted">
      <span class="num">{{ result().strategy_id }}</span> · {{ result().interval }} bars ·
      <span class="num">{{ result().start }} to {{ result().end }}</span>
    </p>
    <div class="tiles" role="group" aria-label="Backtest metrics">
      <app-stat-tile
        class="tile-return"
        label="Total return"
        featured
        [value]="pct(result().final_return, true)"
        [detail]="endValue()"
        [detailTone]="tone(result().final_return)"
      />
      <app-stat-tile label="CAGR" [value]="pct(result().cagr, true)" />
      <app-stat-tile label="Sharpe" [value]="num(result().sharpe, 2)" />
      <app-stat-tile label="Max drawdown" [value]="pct(result().max_drawdown)" />
      <app-stat-tile label="Profit factor" [value]="num(result().profit_factor, 2)" />
      @if (trades() !== null) {
        <app-stat-tile label="Trades" [value]="num(trades(), 0)" />
      }
    </div>
    @if (result().equity.length > 1) {
      <app-time-series-chart
        ariaLabel="Backtest equity, with drawdown below"
        [summary]="summary()"
        [series]="series()"
        [height]="300"
      />
    } @else {
      <p class="muted none">No equity curve: the window had fewer than two bars.</p>
    }
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
    .none {
      font-size: var(--text-sm);
    }
  `,
})
export class BacktestResultView {
  readonly result = input.required<BacktestResult>();

  protected readonly trades = computed(() => {
    const t = (this.result() as BacktestResult & { trades?: unknown }).trades;
    return typeof t === 'number' ? t : null;
  });

  private readonly values = computed(() => this.result().equity.map((p) => p.value));
  private readonly drawdowns = computed(() => drawdownSeries(this.values()));

  protected readonly endValue = computed(() => {
    const v = this.values();
    return v.length ? `${formatMoney(v[0])} to ${formatMoney(v.at(-1))}` : null;
  });

  protected readonly series = computed<ChartSeries[]>(() => {
    const r = this.result();
    const dd = this.drawdowns();
    const times = r.equity.map((p) => chartTime(p.timestamp, r.interval));
    return [
      {
        id: 'equity',
        label: 'Equity',
        kind: 'line',
        color: 'brass',
        format: 'money',
        points: r.equity.map((p, i) => ({ time: times[i], value: p.value })),
      },
      {
        id: 'drawdown',
        label: 'Drawdown',
        kind: 'area',
        color: 'loss',
        pane: 1,
        format: 'percent',
        points: dd.map((value, i) => ({ time: times[i], value })),
      },
    ];
  });

  protected readonly summary = computed(() => {
    const r = this.result();
    const worst = Math.min(0, ...this.drawdowns());
    return (
      `Equity from ${r.start} to ${r.end}: ${this.endValue()}, total return ` +
      `${formatPercent(r.final_return, { signed: true })}. Worst drawdown ${formatPercent(worst)}.`
    );
  });

  protected pct(v: number | null, signed = false): string {
    return formatPercent(v, { signed });
  }

  protected num(v: number | null, digits: number): string {
    return formatNumber(v, { digits });
  }

  protected tone(v: number | null) {
    return toneClass(v);
  }
}
