import { ChangeDetectionStrategy, Component, input } from '@angular/core';

import type {
  BacktestResult,
  BenchmarkStatsView,
  EquityPoint,
  TradeStatsView,
} from '../../api/models';
import { formatMoney, formatNumber, formatPercent, toneClass } from '../../core/format/format';
import { NA } from '../../shared/metrics';
import { HelpTip } from '../../shared/ui/help-tip';

/**
 * Figures for backtest and lab-run results, kept pure so the null handling
 * is easy to test. The API sends non-finite values as `null`: they read
 * "n/a", never 0 or a dash that could pass for "none".
 */
export interface Figure {
  label: string;
  value: string;
  /** Glossary term for the help tip; defaults to the label. */
  help?: string;
  tone?: 'gain' | 'loss' | '';
}

type Num = number | null | undefined;

function finite(v: Num): v is number {
  return typeof v === 'number' && Number.isFinite(v);
}

export function pctOrNa(v: Num, signed = false): string {
  return finite(v) ? formatPercent(v, { signed }) : NA;
}

export function numOrNa(v: Num, digits = 2): string {
  return finite(v) ? formatNumber(v, { digits }) : NA;
}

function moneyOrNa(v: Num): string {
  return finite(v) ? formatMoney(v, { signed: true }) : NA;
}

function barsOrNa(v: Num, digits = 0): string {
  if (!finite(v)) return NA;
  return `${formatNumber(v, { digits })} bar${v === 1 ? '' : 's'}`;
}

function multipleOrNa(v: Num): string {
  return finite(v) ? `${formatNumber(v, { digits: 2 })}×` : NA;
}

/** Risk beyond Sharpe and max drawdown. */
export function riskFigures(r: BacktestResult): Figure[] {
  return [
    { label: 'Sortino', value: numOrNa(r.sortino) },
    { label: 'Calmar', value: numOrNa(r.calmar) },
    { label: 'Ulcer index', value: numOrNa(r.ulcer_index, 3) },
    { label: 'VaR 95%', value: pctOrNa(r.var_95), help: 'var' },
    { label: 'ES 95%', value: pctOrNa(r.es_95), help: 'expected_shortfall' },
    {
      label: 'Longest drawdown',
      value: barsOrNa(r.max_dd_duration_bars),
      help: 'drawdown_duration',
    },
  ];
}

/** Trade-level statistics over closed round trips. */
export function tradeFigures(t: TradeStatsView): Figure[] {
  const trades =
    t.n_open > 0 ? `${formatNumber(t.n_trades, { digits: 0 })} + ${t.n_open} open` : null;
  return [
    {
      label: 'Trades',
      value: trades ?? formatNumber(t.n_trades, { digits: 0 }),
    },
    { label: 'Win rate', value: pctOrNa(t.win_rate, false) },
    { label: 'Expectancy', value: moneyOrNa(t.expectancy), tone: toneClass(t.expectancy) },
    // No losing trades makes the ratio unbounded; the API sends null.
    { label: 'Payoff ratio', value: numOrNa(t.payoff_ratio) },
    { label: 'Avg holding time', value: barsOrNa(t.avg_bars_held, 1), help: 'holding_time' },
    { label: 'Turnover', value: multipleOrNa(t.turnover_annual), help: 'turnover' },
    { label: 'Cost drag', value: pctOrNa(t.cost_drag_annual), help: 'cost_drag' },
  ];
}

/** The strategy against its benchmark. */
export function benchmarkFigures(b: BenchmarkStatsView): Figure[] {
  return [
    {
      label: 'Excess CAGR',
      value: pctOrNa(b.excess_cagr, true),
      tone: toneClass(b.excess_cagr),
    },
    { label: 'Alpha', value: pctOrNa(b.alpha_annual, true), help: 'alpha' },
    { label: 'Beta', value: numOrNa(b.beta) },
    { label: 'Information ratio', value: numOrNa(b.information_ratio) },
    { label: 'Up capture', value: multipleOrNa(b.up_capture), help: 'capture_ratio' },
    { label: 'Down capture', value: multipleOrNa(b.down_capture), help: 'capture_ratio' },
    { label: 'Tracking error', value: pctOrNa(b.tracking_error) },
    { label: 'Benchmark CAGR', value: pctOrNa(b.benchmark_cagr, true), help: 'cagr' },
  ];
}

/** "SPY.US", "Equal-weight universe (4 tickers)". */
export function benchmarkName(b: BenchmarkStatsView): string {
  if (b.spec.toUpperCase() === 'EW' || b.name.toUpperCase() === 'EW') {
    const n = b.members?.length ?? 0;
    return n ? `Equal-weight universe (${n} tickers)` : 'Equal-weight universe';
  }
  return b.name || b.spec;
}

/** Values rebased so the first point is `base` (both lines start at 100). */
export function rebase(points: readonly EquityPoint[], base = 100): number[] {
  const first = points.find((p) => finite(p.value) && p.value !== 0)?.value;
  if (!first) return points.map(() => base);
  return points.map((p) => (p.value / first) * base);
}

/**
 * A grid of labelled figures with help tips; wraps to two columns on
 * phones.
 *
 *   <app-figure-grid label="Risk" [figures]="risk()" />
 */
@Component({
  selector: 'app-figure-grid',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [HelpTip],
  template: `
    <dl [attr.aria-label]="label()">
      @for (f of figures(); track f.label) {
        <div>
          <dt>{{ f.label }} <app-help-tip [term]="f.help ?? f.label" /></dt>
          <dd class="num" [class]="f.tone ?? ''" [class.na]="f.value === na">{{ f.value }}</dd>
        </div>
      }
    </dl>
  `,
  styles: `
    @use 'breakpoints' as bp;
    :host {
      display: block;
      min-width: 0;
    }
    dl {
      margin: 0;
      display: grid;
      gap: var(--space-2);
      grid-template-columns: repeat(2, minmax(0, 1fr));
      @include bp.from-tablet {
        grid-template-columns: repeat(auto-fill, minmax(9.5rem, 1fr));
      }
    }
    div {
      display: grid;
      align-content: start;
      gap: 2px;
      min-width: 0;
      padding: var(--space-2) var(--space-3);
      border-radius: var(--radius-sm);
      background: var(--color-surface-2);
    }
    dt {
      font-size: var(--text-xs);
      color: var(--color-ink-3);
    }
    dd {
      margin: 0;
      font-weight: var(--weight-semibold);
      overflow-wrap: anywhere;
    }
    dd.gain {
      color: var(--color-gain);
    }
    dd.loss {
      color: var(--color-loss);
    }
    dd.na {
      color: var(--color-ink-3);
      font-weight: var(--weight-regular);
    }
  `,
})
export class FigureGrid {
  readonly label = input.required<string>();
  readonly figures = input.required<readonly Figure[]>();
  protected readonly na = NA;
}
