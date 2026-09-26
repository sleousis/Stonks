import { TestBed } from '@angular/core/testing';

import { FakeChartEngine, provideFakeChart } from '../../../testing/fake-chart';
import { tick } from '../../../testing/http';
import { CATEGORICAL_LINES, CHART_ENGINE, type ChartSeries } from './chart-engine';
import { TimeSeriesChart } from './time-series-chart';

const SERIES: ChartSeries[] = [
  {
    id: 'a',
    label: 'momentum-v3',
    kind: 'line',
    color: 'primary',
    points: [{ time: '2026-09-25', value: 101 }],
  },
  {
    id: 'b',
    label: 'value-v1',
    kind: 'line',
    color: 'info',
    dashed: true,
    points: [{ time: '2026-09-25', value: 99 }],
  },
];

async function render() {
  const fixture = TestBed.createComponent(TimeSeriesChart);
  fixture.componentRef.setInput('series', SERIES);
  fixture.componentRef.setInput('ariaLabel', 'Two strategies');
  fixture.componentRef.setInput('summary', 'momentum-v3 ends at 101, value-v1 at 99.');
  await tick();
  await fixture.whenStable();
  fixture.detectChanges();
  return fixture.nativeElement as HTMLElement;
}

describe('TimeSeriesChart', () => {
  it('draws the series and names each line in the legend', async () => {
    const engine = new FakeChartEngine();
    TestBed.configureTestingModule({ providers: [provideFakeChart(engine)] });
    const el = await render();
    expect(engine.last?.map((s) => s.id)).toEqual(['a', 'b']);
    const items = [...el.querySelectorAll('.legend .item')].map((i) => i.textContent?.trim());
    expect(items[0]).toContain('momentum-v3');
    expect(items[1]).toContain('value-v1');
    expect(el.querySelector('.swatch.dashed')?.getAttribute('data-color')).toBe('info');
    expect(el.querySelector('.failed')).toBeNull();
  });

  it('engine load failure shows the summary and a reload hint', async () => {
    const error = vi.spyOn(console, 'error').mockImplementation(() => undefined);
    TestBed.configureTestingModule({
      providers: [
        { provide: CHART_ENGINE, useValue: () => Promise.reject(new Error('chunk failed')) },
      ],
    });
    const el = await render();
    const failed = el.querySelector('.failed');
    expect(failed?.getAttribute('role')).toBe('alert');
    expect(failed?.textContent).toContain('momentum-v3 ends at 101, value-v1 at 99.');
    expect(failed?.textContent).toContain('The chart could not load. Reload the page.');
    expect(el.querySelector('.plot')?.classList).toContain('gone');
    error.mockRestore();
  });

  it('offers categorical line styles that never use gain, loss or brass', () => {
    const colors = CATEGORICAL_LINES.map((l) => l.color);
    expect(colors).not.toContain('gain');
    expect(colors).not.toContain('loss');
    expect(colors).not.toContain('brass');
    const keys = CATEGORICAL_LINES.map((l) => `${l.color}:${l.dashed}`);
    expect(new Set(keys).size).toBe(keys.length);
  });
});
