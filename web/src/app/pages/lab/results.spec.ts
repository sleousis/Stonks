import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import { FakeChartEngine, provideFakeChart } from '../../../testing/fake-chart';
import { tick } from '../../../testing/http';
import { BACKTEST_RESULT, LAB_RUN_VIEW } from '../../../testing/lab-fixtures';
import { BacktestResultView, chartTime, drawdownSeries } from './backtest-result';
import { LabRunResultView, formatMetric } from './lab-run-result';

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
    expect(equity).toMatchObject({ id: 'equity', color: 'brass', kind: 'line' });
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
    fixture.componentRef.setInput('result', { ...BACKTEST_RESULT, trades: 14 });
    fixture.detectChanges();
    expect(tiles(fixture.nativeElement)['Trades']).toBe('14');
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
    expect(tiles(el)).toEqual({ Verdict: 'Failed', 'Best score': '1.12' });
    expect(el.textContent).toContain('2 of 3 tests passed');
    const params = [...el.querySelectorAll('.params div')].map((d) =>
      [d.querySelector('dt')!.textContent!.trim(), d.querySelector('dd')!.textContent!.trim()].join(
        ' ',
      ),
    );
    expect(params).toEqual(['Lookback days 60', 'Mode slow', 'Long only true']);
  });

  it('renders one pass/fail row per survival test with metrics and notes', () => {
    const el = render();
    const rows = [...el.querySelectorAll('.test')];
    expect(rows.map((r) => r.querySelector('.test-name')!.textContent!.trim())).toEqual([
      'Out of sample',
      'Walk-forward',
      'Monte Carlo permutation',
    ]);
    expect(rows.map((r) => r.querySelector('app-status-pill')!.textContent!.trim())).toEqual([
      'pass',
      'fail',
      'pass',
    ]);
    expect(rows[1].classList).toContain('failed');
    expect(rows[1].textContent).toContain('Positive share');
    expect(rows[1].textContent).toContain('25.00%');
    expect(rows[1].textContent).toContain('1 of 4 folds positive.');
    expect(rows[2].textContent).toContain('0.02');
    expect(rows[2].querySelector('.notes')).toBeNull();
  });

  it('links a registered strategy', () => {
    const el = render({ ...LAB_RUN_VIEW, verdict: 'pass', registered_strategy_id: 'momentum-7' });
    const link = el.querySelector<HTMLAnchorElement>('.registered a')!;
    expect(link.textContent).toBe('momentum-7');
    expect(link.getAttribute('href')).toBe('/strategies/momentum-7');
  });

  it('formats metrics by kind', () => {
    expect(formatMetric('oos_return', 0.1)).toBe('10.00%');
    expect(formatMetric('p_value', 0.01234)).toBe('0.012');
    expect(formatMetric('n_splits', 4)).toBe('4');
    expect(formatMetric('score', null)).toBe('–');
  });
});
