import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import { LAB_RUN_VIEW } from '../../../testing/lab-fixtures';
import type { HeatmapView } from '../../api/models';
import { LabRunResultView } from './lab-run-result';
import { ParamHeatmap, cellTint, heatmapMetricText, heatmapRows } from './param-heatmap';

const HEATMAP: HeatmapView = {
  x: 'lookback_days',
  y: 'threshold',
  x_values: [10, 20, 30],
  y_values: [0, 0.1],
  scores: [
    [0.2, 0.9, null],
    [-0.4, 0.6, 0.5],
  ],
  metric: 'fast_sharpe',
  fast: true,
  best: { lookback_days: 20, threshold: 0 },
  fixed: { mode: 'slow' },
  plateau: {
    passed: false,
    notes: 'Neighbours fall off.',
    step: 0.1,
    x_range: [15, 25],
    y_range: [0, 0.05],
    metrics: { ratio: 0.42 },
  },
};

describe('ParamHeatmap', () => {
  function render(h: HeatmapView = HEATMAP): HTMLElement {
    const fixture = TestBed.createComponent(ParamHeatmap);
    fixture.componentRef.setInput('heatmap', h);
    fixture.detectChanges();
    return fixture.nativeElement;
  }

  it('draws a table with x across and y down, n/a for failed cells', () => {
    const el = render();
    expect(el.querySelector('caption')?.textContent).toContain(
      'Sharpe on the fast path by Lookback days (across) and Threshold (down)',
    );
    const heads = [...el.querySelectorAll('thead th')].map((t) => t.textContent!.trim());
    expect(heads.slice(1)).toEqual(['10', '20', '30']);
    const rows = [...el.querySelectorAll('tbody tr')];
    expect(rows).toHaveLength(2);
    const cells = [...rows[0].querySelectorAll('td')];
    expect(cells[2].textContent).toContain('n/a');
    expect(cells[2].classList).toContain('na');
    expect(el.querySelector('.wrap')?.getAttribute('role')).toBe('region');
    expect(el.querySelector('.wrap')?.getAttribute('tabindex')).toBe('0');
  });

  it('outlines the tuned cell and marks the plateau neighbourhood', () => {
    const el = render();
    const best = el.querySelectorAll('td.best');
    expect(best).toHaveLength(1);
    expect(best[0].textContent).toContain('0.9');
    expect(best[0].getAttribute('aria-label')).toContain('the tuned set');
    expect(best[0].classList).toContain('plateau');
    expect(el.querySelectorAll('td.plateau')).toHaveLength(1);
  });

  it('shows the plateau verdict, the metric, what was held and the trial note', () => {
    const el = render();
    const text = el.textContent!;
    expect(text).toContain('Plateau test');
    expect(el.querySelector('.plateau')?.classList).toContain('failed');
    expect(text).toContain('Neighbours fall off.');
    expect(text).toContain('10%');
    expect(text).toContain('Mode slow');
    expect(text).toContain('Every cell counts as a trial');
  });

  it('works without a plateau overlay', () => {
    const el = render({ ...HEATMAP, plateau: null, fast: false });
    expect(el.querySelector('.plateau')).toBeNull();
    expect(el.querySelectorAll('td.plateau')).toHaveLength(0);
    expect(el.textContent).toContain('with full backtests');
  });

  it('tints gain for high and loss for low, centred on 0 across signs', () => {
    expect(cellTint(1, -1, 1)).toContain('--color-gain');
    expect(cellTint(-1, -1, 1)).toContain('--color-loss');
    expect(cellTint(0, -1, 1)).toBe('');
    expect(cellTint(1, 1, 1)).toBe('');
    // All positive: centred on the middle of the range.
    expect(cellTint(1, 1, 3)).toContain('--color-loss');
    expect(heatmapRows(HEATMAP)[1].cells[0].background).toContain('--color-loss');
  });

  it('names the metrics in words', () => {
    expect(heatmapMetricText('sharpe_dd')).toBe('Sharpe less twice the drawdown');
    expect(heatmapMetricText('calmar')).toBe('Calmar');
  });
});

describe('LabRunResultView heatmap section', () => {
  it('shows the heatmap only when the run drew one', () => {
    TestBed.configureTestingModule({ providers: [provideRouter([])] });
    const fixture = TestBed.createComponent(LabRunResultView);
    fixture.componentRef.setInput('result', LAB_RUN_VIEW);
    fixture.detectChanges();
    const el: HTMLElement = fixture.nativeElement;
    expect(el.querySelector('app-param-heatmap')).toBeNull();
    fixture.componentRef.setInput('result', { ...LAB_RUN_VIEW, heatmap: HEATMAP });
    fixture.detectChanges();
    expect(el.querySelector('app-param-heatmap')).not.toBeNull();
    expect(el.textContent).toContain('Parameter heatmap');
  });
});
