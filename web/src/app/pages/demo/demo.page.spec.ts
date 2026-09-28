import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import type { DemoPortfolioView } from '../../api/models';
import { provideApi } from '../../api/provide-api';
import { ConfirmService } from '../../core/confirm/confirm.service';
import { MONEY_MASK, moneyHidden } from '../../core/format/format';
import { provideFakeChart } from '../../../testing/fake-chart';
import { nextRequest, tick } from '../../../testing/http';
import { DemoPage } from './demo.page';

const DEMO: DemoPortfolioView = {
  exists: true,
  label: 'Sample data',
  name: 'Demo portfolio',
  as_of: '2026-09-28',
  currency: 'USD',
  start_value: 100000,
  cash: 18000,
  total_value: 112000,
  total_return: 0.12,
  day_change: 500,
  positions: [
    {
      ticker: 'ACME.DEMO',
      name: 'Acme Robotics (sample)',
      sector: 'Technology',
      quantity: 100,
      cost: 200,
      price: 250,
      value: 25000,
      weight: 0.223,
      pnl: 5000,
      pnl_pct: 0.25,
    },
  ],
  curve: [
    { day: '2026-09-25', value: 111500 },
    { day: '2026-09-28', value: 112000 },
  ],
  created_at: '2026-09-28T09:00:00Z',
};

describe('DemoPage', () => {
  let http: HttpTestingController;
  const confirm = vi.fn();

  beforeEach(() => {
    confirm.mockReset();
    moneyHidden.set(false);
    TestBed.configureTestingModule({
      providers: [
        provideRouter([]),
        ...provideApi(),
        provideHttpClientTesting(),
        provideFakeChart(),
        { provide: ConfirmService, useValue: { confirm } },
      ],
    });
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    http.verify();
    moneyHidden.set(false);
  });

  async function render(view: DemoPortfolioView) {
    const fixture = TestBed.createComponent(DemoPage);
    fixture.detectChanges();
    (await nextRequest(http, '/api/demo')).flush(view);
    await tick();
    fixture.detectChanges();
    return fixture;
  }

  it('offers to open a demo, then shows it labelled as sample data', async () => {
    const fixture = await render({ exists: false, label: 'Sample data' } as DemoPortfolioView);
    const root = fixture.nativeElement as HTMLElement;
    expect(root.textContent).toContain('See Stonks with sample data');
    const open = [...root.querySelectorAll('button')].find((b) =>
      b.textContent?.includes('Open the demo'),
    )!;
    open.click();
    (await nextRequest(http, '/api/demo', 'POST')).flush(DEMO);
    await tick();
    fixture.detectChanges();
    expect(root.querySelector('[role="note"]')?.textContent).toContain('Sample data');
    expect(root.textContent).toContain('ACME.DEMO');
    expect(root.textContent).toContain('$112,000.00');
  });

  it('hides money in privacy mode but keeps the return', async () => {
    moneyHidden.set(true);
    const fixture = await render(DEMO);
    const root = fixture.nativeElement as HTMLElement;
    expect(root.textContent).toContain(MONEY_MASK);
    expect(root.textContent).not.toContain('$112,000.00');
    expect(root.textContent).toContain('12.00%');
  });

  it('removes the demo after asking', async () => {
    confirm.mockResolvedValue(true);
    const fixture = await render(DEMO);
    const root = fixture.nativeElement as HTMLElement;
    const remove = [...root.querySelectorAll('button')].find((b) =>
      b.textContent?.includes('Remove the demo'),
    )!;
    remove.click();
    await tick();
    (await nextRequest(http, '/api/demo', 'DELETE')).flush(null, {
      status: 204,
      statusText: 'No Content',
    });
    await tick();
    fixture.detectChanges();
    expect(confirm).toHaveBeenCalledOnce();
    expect(root.textContent).toContain('See Stonks with sample data');
  });
});
