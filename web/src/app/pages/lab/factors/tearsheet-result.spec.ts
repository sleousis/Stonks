import { type ComponentFixture, TestBed } from '@angular/core/testing';

import { FakeChartEngine, provideFakeChart } from '../../../../testing/fake-chart';
import { tick } from '../../../../testing/http';
import type { FactorTearSheetView } from '../../../api/models';
import { TEARSHEET } from './factor-test-fixtures';
import { FactorTearsheetResult, barsText, bucketLabel } from './tearsheet-result';

describe('FactorTearsheetResult', () => {
  let fixture: ComponentFixture<FactorTearsheetResult>;
  let engine: FakeChartEngine;
  let el: HTMLElement;

  async function render(view: FactorTearSheetView): Promise<void> {
    engine = new FakeChartEngine();
    TestBed.configureTestingModule({
      imports: [FactorTearsheetResult],
      providers: [provideFakeChart(engine)],
    });
    fixture = TestBed.createComponent(FactorTearsheetResult);
    fixture.componentRef.setInput('result', view);
    el = fixture.nativeElement;
    fixture.detectChanges();
    await tick(5);
    fixture.detectChanges();
  }

  function section(title: string): HTMLElement {
    const heading = [...el.querySelectorAll('h3')].find((h) => h.textContent!.trim() === title);
    if (!heading) throw new Error(`no section ${title}`);
    return heading.closest('section')!;
  }

  it('labels buckets and horizons plainly', () => {
    expect(bucketLabel(0, 5)).toBe('Q1 (lowest)');
    expect(bucketLabel(2, 5)).toBe('Q3');
    expect(bucketLabel(4, 5)).toBe('Q5 (highest)');
    expect(barsText(1)).toBe('1 bar');
    expect(barsText(21)).toBe('21 bars');
  });

  it('shows the tiles from the main horizon and the alpha', async () => {
    await render(TEARSHEET);
    const tiles = el.querySelector('.tiles')!.textContent!;
    expect(tiles).toContain('0.042');
    expect(tiles).toContain('Next 21 bars');
    expect(tiles).toContain('t-stat 2.3');
    expect(tiles).toContain('40');
    expect(tiles).toContain('150');
    expect(el.textContent).toContain('mom_12_1: higher is better.');
  });

  it('lists IC per horizon in order, with n/a for missing figures', async () => {
    await render(TEARSHEET);
    const rows = [...section('IC per horizon').querySelectorAll('tbody tr')].map(
      (r) => r.textContent!,
    );
    expect(rows[0]).toContain('1 bar');
    expect(rows[0]).toContain('n/a');
    expect(rows[1]).toContain('21 bars');
    expect(rows[1]).toContain('0.042');
    expect(rows[1]).toContain('0.42');
  });

  it('draws a bar per bucket, a negative one leftwards from the middle', async () => {
    await render(TEARSHEET);
    const rows = section('Return per bucket').querySelectorAll('tbody tr');
    expect(rows.length).toBe(3);
    expect(rows[0].querySelector('th')!.textContent).toContain('Q1 (lowest)');
    const low = rows[0].querySelector<HTMLElement>('.bar')!;
    expect(low.classList).toContain('neg');
    const high = rows[2].querySelector<HTMLElement>('.bar')!;
    expect(high.style.width).toBe('50%');
    expect(high.style.marginLeft).toBe('50%');
  });

  it('draws each bucket and the spread as lines', async () => {
    await render(TEARSHEET);
    const series = engine.last!;
    expect(series.map((s) => s.id)).toEqual(['q1', 'q2', 'q3', 'spread']);
    expect(series[0].color).toBe('loss');
    expect(series[2].color).toBe('gain');
    expect(series[3].points.at(-1)).toEqual({ time: '2023-01-23', value: 0.046 });
  });

  it('shows IC by group, skipping empty groups, and the monthly heatmap', async () => {
    await render(TEARSHEET);
    const groups = section('IC by group');
    expect(groups.querySelectorAll('app-data-table').length).toBe(1);
    expect(groups.textContent).toContain('Technology');
    expect(groups.textContent).toContain('dollar volume');
    const cells = section('Monthly IC').querySelectorAll<HTMLElement>('tbody td');
    expect(cells.length).toBe(12);
    expect(cells[0].textContent!.trim()).toBe('0.05');
    expect(cells[0].getAttribute('style')).toContain('--color-gain');
    expect(cells[1].getAttribute('style')).toContain('--color-loss');
    expect(cells[2].classList).toContain('na');
  });

  it('explains a sheet with too little data', async () => {
    await render({
      ...TEARSHEET,
      status: 'n/a',
      note: 'Fewer than 10 names.',
      horizons: [],
      ic_by_group: {},
      monthly_ic: [],
      quantile_curves: { dates: [], series: [], spread: [] },
    });
    expect(el.querySelector('.note')!.textContent).toContain(
      'Not enough data for a tear sheet. Fewer than 10 names.',
    );
    expect(el.querySelectorAll('h3').length).toBe(1); // only alpha and beta
  });
});
