import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import { FakeChartEngine, provideFakeChart } from '../../../testing/fake-chart';
import { tick } from '../../../testing/http';
import {
  BACKTEST_RESULT,
  BACKTEST_RESULT_FULL,
  LAB_RUN_VIEW,
  LAB_RUN_VIEW_FULL,
} from '../../../testing/lab-fixtures';
import { BacktestResultView, chartTime, drawdownSeries } from './backtest-result';
import { LabRunResultView, formatMetric } from './lab-run-result';
import { rebase } from './result-figures';

function tiles(el: HTMLElement): Record<string, string> {
  return Object.fromEntries(
    [...el.querySelectorAll('app-stat-tile')].map((t) => [
      t.querySelector('.label')!.textContent!.trim(),
      t.querySelector('.value')!.textContent!.trim(),
    ]),
  );
}

describe('BacktestResultView', () => {
  it('computes drawdown from the running peak', () => {
    expect(drawdownSeries([100, 110, 99, 121])).toEqual([0, 0, 99 / 110 - 1, 0]);
    expect(chartTime('2026-01-02T00:00:00', '1d')).toBe('2026-01-02');
    expect(chartTime('2026-01-02T14:30:00', '5m')).toBe('2026-01-02T14:30:00');
  });

  it('renders metric tiles and an equity + drawdown chart', async () => {
    const engine = new FakeChartEngine();
    TestBed.configureTestingModule({ providers: [provideFakeChart(engine)] });
    const fixture = TestBed.createComponent(BacktestResultView);
    fixture.componentRef.setInput('result', BACKTEST_RESULT);
    fixture.detectChanges();
    await tick();
    fixture.detectChanges();
    const el = fixture.nativeElement as HTMLElement;

    expect(tiles(el)).toEqual({
      'Total return': '+4.20%',
      CAGR: '+18.00%',
      Sharpe: '1.35',
      'Max drawdown': '-5.00%',
      'Profit factor': '1.8',
    });
    expect(el.textContent).toContain('$10,000.00 to $10,420.00');

    const [equity, drawdown] = engine.last!;
    expect(equity).toMatchObject({ id: 'equity', color: 'primary', kind: 'line' });
    expect(equity.points.map((p) => p.time)).toEqual([
      '2026-01-02',
      '2026-01-05',
      '2026-01-06',
      '2026-01-07',
    ]);
    expect(drawdown).toMatchObject({ id: 'drawdown', color: 'loss', kind: 'area', pane: 1 });
    expect(drawdown.points[2].value).toBeCloseTo(-0.05);
  });

  it('shows trades when the result carries them', () => {
    TestBed.configureTestingModule({ providers: [provideFakeChart()] });
    const fixture = TestBed.createComponent(BacktestResultView);
    fixture.componentRef.setInput('result', { ...BACKTEST_RESULT, trade_count: 14 });
    fixture.detectChanges();
    expect(tiles(fixture.nativeElement)['Trades']).toBe('14');
  });
});

function figures(section: Element | null): Record<string, string> {
  if (!section) throw new Error('section missing');
  return Object.fromEntries(
    [...section.querySelectorAll('dl > div')].map((d) => [
      d.querySelector('dt')!.textContent!.trim(),
      d.querySelector('dd')!.textContent!.trim(),
    ]),
  );
}

describe('BacktestResultView with Phase 9 figures', () => {
  async function render() {
    const engine = new FakeChartEngine();
    TestBed.configureTestingModule({ providers: [provideFakeChart(engine)] });
    const fixture = TestBed.createComponent(BacktestResultView);
    fixture.componentRef.setInput('result', BACKTEST_RESULT_FULL);
    fixture.detectChanges();
    await tick();
    fixture.detectChanges();
    return { el: fixture.nativeElement as HTMLElement, engine };
  }

  it('shows risk and trade figures, nulls as n/a', async () => {
    const { el } = await render();
    expect(tiles(el)['Trades']).toBe('12');
    expect(figures(el.querySelector('[aria-labelledby="bt-risk-title"]'))).toEqual({
      Sortino: '2.1',
      Calmar: 'n/a',
      'Ulcer index': '0.012',
      'VaR 95%': '-2.10%',
      'ES 95%': 'n/a',
      'Longest drawdown': '3 bars',
    });
    expect(figures(el.querySelector('[aria-labelledby="bt-trades-title"]'))).toEqual({
      Trades: '12 + 1 open',
      'Win rate': '58.33%',
      Expectancy: '+$36.67',
      'Payoff ratio': 'n/a',
      'Avg holding time': '4.5 bars',
      Turnover: '6.2×',
      'Cost drag': '0.31%',
    });
  });

  it('compares with the benchmark, both rebased to 100 on one chart', async () => {
    const { el, engine } = await render();
    const bench = figures(el.querySelector('[aria-labelledby="bt-bench-title"]'));
    expect(bench['Excess CAGR']).toBe('+3.10%');
    expect(bench['Alpha']).toBe('+5.20%');
    expect(bench['Beta']).toBe('0.8');
    expect(bench['Information ratio']).toBe('n/a');
    expect(bench['Up capture']).toBe('1.1×');
    expect(bench['Down capture']).toBe('0.7×');
    expect(el.textContent).toContain('SPY.US');

    const [equity, benchmark, drawdown] = engine.last!;
    expect(equity).toMatchObject({ id: 'equity', format: 'number' });
    expect(equity.points[0].value).toBe(100);
    expect(equity.points[3].value).toBeCloseTo(104.2);
    expect(benchmark).toMatchObject({ id: 'benchmark', color: 'muted' });
    expect(benchmark.pane).toBeUndefined();
    expect(benchmark.points[3].value).toBeCloseTo(102);
    // The API's drawdown series is used as sent.
    expect(drawdown.points[3].value).toBeCloseTo(-0.0076);
    expect(rebase([{ timestamp: 't', value: 0 }])).toEqual([100]);
  });

  it('says when there is no benchmark or trade statistics', async () => {
    TestBed.configureTestingModule({ providers: [provideFakeChart()] });
    const fixture = TestBed.createComponent(BacktestResultView);
    fixture.componentRef.setInput('result', BACKTEST_RESULT);
    fixture.detectChanges();
    const el = fixture.nativeElement as HTMLElement;
    expect(el.textContent).toContain('No benchmark for this run');
    expect(el.textContent).toContain('No trade statistics');
    expect(figures(el.querySelector('[aria-labelledby="bt-risk-title"]'))['Sortino']).toBe('n/a');
  });
});

describe('LabRunResultView', () => {
  function render(view = LAB_RUN_VIEW) {
    TestBed.configureTestingModule({ providers: [provideRouter([])] });
    const fixture = TestBed.createComponent(LabRunResultView);
    fixture.componentRef.setInput('result', view);
    fixture.detectChanges();
    return fixture.nativeElement as HTMLElement;
  }

  it('renders the verdict, best score and best params', () => {
    const el = render();
    expect(tiles(el)).toEqual({
      'Robustness verdict': 'Failed',
      'Best score': '1.12',
      // Older results carry no trial counts.
      'Trials this run': 'n/a',
      'Trials of this strategy': 'n/a',
    });
    expect(el.textContent).toContain('2 of 3 robustness tests passed');
    // The verdict in words, naming what failed, and why trials are not the verdict.
    const plain = el.querySelector('.plain')!.textContent!;
    expect(plain).toContain('It did not hold up: it failed 1 of 3 robustness tests (Walk-forward)');
    expect(plain).toContain('A trial only says a setting ran');
    const params = [...el.querySelectorAll('.params div')].map((d) =>
      [d.querySelector('dt')!.textContent!.trim(), d.querySelector('dd')!.textContent!.trim()].join(
        ' ',
      ),
    );
    expect(params).toEqual(['Lookback days 60', 'Mode slow', 'Long only true']);
  });

  it('shows preflight warnings when the run has any, and nothing when it has none', () => {
    expect(render().querySelector('app-preflight-issues')).toBeNull();
    TestBed.resetTestingModule();
    const el = render({
      ...LAB_RUN_VIEW,
      preflight: {
        ok: true,
        skipped: false,
        issues: [
          {
            code: 'survivorship_bias',
            severity: 'warning',
            message: 'The list has no dated spans.',
            details: { tickers: ['AAPL.US', 'MSFT.US'] },
          },
        ],
      },
    });
    const issue = el.querySelector('app-preflight-issues .issue')!;
    expect(issue.textContent).toContain('Survivorship bias');
    expect(issue.textContent).toContain('The list has no dated spans.');
    expect(issue.textContent).toContain('Tickers: AAPL.US, MSFT.US');
    expect(issue.querySelector('app-status-pill')!.textContent!.trim()).toBe('Warning');
  });

  it('renders one pass/fail row per survival test with metrics and notes', () => {
    const el = render();
    const rows = [...el.querySelectorAll('.test')];
    expect(rows.map((r) => r.querySelector('.test-name')!.textContent!.trim())).toEqual([
      'Out of sample',
      'Walk-forward',
      'Shuffled prices (MCPT)',
    ]);
    // Each test says what it guards against.
    expect(rows[0].querySelector('.hint')!.textContent).toContain('Guards against');
    expect(rows.map((r) => r.querySelector('app-status-pill')!.textContent!.trim())).toEqual([
      'Passed',
      'Failed',
      'Passed',
    ]);
    expect(rows[1].classList).toContain('failed');
    expect(rows[1].textContent).toContain('Positive folds');
    expect(rows[0].textContent).toContain('Out-of-sample score');
    expect(rows[1].textContent).toContain('25.00%');
    expect(rows[1].textContent).toContain('1 of 4 folds positive.');
    expect(rows[2].textContent).toContain('0.02');
    expect(rows[2].querySelector('.notes')).toBeNull();
  });

  it('links a registered strategy', () => {
    const el = render({ ...LAB_RUN_VIEW, verdict: 'pass', registered_strategy_id: 'momentum-7' });
    expect(el.querySelector('.plain')!.textContent).toContain('It held up');
    const link = el.querySelector<HTMLAnchorElement>('.registered a')!;
    expect(el.querySelector('.registered')!.textContent).toContain('Put on trial as');
    expect(link.textContent).toBe('momentum-7');
    expect(link.getAttribute('href')).toBe('/strategies/momentum-7');
  });

  it('formats metrics by kind', () => {
    expect(formatMetric('oos_return', 0.1)).toBe('10.00%');
    expect(formatMetric('p_value', 0.01234)).toBe('0.012');
    expect(formatMetric('n_splits', 4)).toBe('4');
    expect(formatMetric('score', null)).toBe('n/a');
    expect(formatMetric('p95_max_dd', 0.18)).toBe('18.00%');
    expect(formatMetric('return_to_dd', 2.4)).toBe('2.4×');
    expect(formatMetric('break_even_multiple', 4.5)).toBe('4.5×');
    expect(formatMetric('used_realistic_costs', 1)).toBe('Yes');
  });

  it('shows trial counts, the benchmark and the deciding figures of each test', () => {
    const el = render(LAB_RUN_VIEW_FULL);
    const t = tiles(el);
    expect(t['Trials this run']).toBe('40');
    expect(t['Trials of this strategy']).toBe('180');
    expect(figures(el.querySelector('[aria-labelledby="lr-bench-title"]'))['Excess CAGR']).toBe(
      '+3.10%',
    );

    const rows = [...el.querySelectorAll('.test')];
    expect(rows[0].querySelector(':scope > dl')!.textContent).toContain('Deflated Sharpe');
    expect(rows[0].querySelector(':scope > dl')!.textContent).toContain('0.971');
    expect(rows[1].querySelector(':scope > dl')!.textContent).toContain('PBO');
    expect(rows[1].querySelector(':scope > dl')!.textContent).toContain('n/a');
    expect(rows[2].querySelector(':scope > dl')!.textContent).toContain(
      '95th percentile drawdown (MC)',
    );
    expect(rows[2].querySelector(':scope > dl')!.textContent).toContain('18.00%');
    expect(rows[3].querySelector(':scope > dl')!.textContent).toContain('Break-even cost multiple');
    expect(rows[3].querySelector(':scope > dl')!.textContent).toContain('Sharpe at 2× costs');
    expect(rows[4].querySelector(':scope > dl')!.textContent).toContain('Walk-forward efficiency');
    expect(rows[4].querySelector(':scope > dl')!.textContent).toContain('0.72');
    // Secondary figures are folded away.
    expect(rows[0].querySelector('details')?.textContent).toContain('Var sr');
  });
});
