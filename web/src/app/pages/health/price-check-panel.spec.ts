import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { type ComponentFixture, TestBed } from '@angular/core/testing';

import type { PriceCheckView } from '../../api/models';
import { provideApi } from '../../api/provide-api';
import { nextRequest, tick } from '../../../testing/http';
import { PriceCheckPanel, notableItems, priceCheckState } from './price-check-panel';

function check(over: Partial<PriceCheckView> = {}): PriceCheckView {
  return {
    id: 1,
    as_of: '2026-09-25',
    checked_at: '2026-09-25T20:42:00+00:00',
    source: 'yahoo',
    status: 'clean',
    tickers_checked: 3,
    tickers_compared: 3,
    held: [],
    items: [
      { ticker: 'A.US', status: 'ok', detail: 'matches', source: 'yahoo' },
      { ticker: 'B.US', status: 'ok', detail: 'matches', source: 'yahoo' },
    ],
    detail: '0 of 3 compared tickers differ from yahoo',
    halt_id: null,
    ...over,
  };
}

const GAPS = check({
  status: 'gaps',
  held: ['B.US'],
  items: [
    { ticker: 'Z.US', status: 'unknown', detail: 'yahoo has no price', source: 'yahoo' },
    { ticker: 'B.US', status: 'gap', detail: 'close 9.1% off (limit 2.0%)', source: 'yahoo' },
    { ticker: 'A.US', status: 'ok', detail: 'matches', source: 'yahoo' },
  ],
});

describe('price check words', () => {
  it('names each status and puts gaps first', () => {
    expect(priceCheckState(check()).label).toBe('Prices agree');
    expect(priceCheckState(GAPS).tone).toBe('warn');
    expect(priceCheckState(check({ status: 'systematic' })).status).toBe('halted');
    expect(priceCheckState(check({ status: 'unavailable' })).label).toBe('Nothing compared');
    expect(notableItems(GAPS).map((i) => i.ticker)).toEqual(['B.US', 'Z.US']);
  });
});

describe('PriceCheckPanel', () => {
  let fixture: ComponentFixture<PriceCheckPanel>;
  let http: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [...provideApi(), provideHttpClientTesting()],
    });
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  async function render(body: PriceCheckView | null): Promise<HTMLElement> {
    fixture = TestBed.createComponent(PriceCheckPanel);
    fixture.detectChanges();
    (await nextRequest(http, '/api/health/price-check')).flush(body);
    await tick();
    fixture.detectChanges();
    return fixture.nativeElement as HTMLElement;
  }

  it('stays hidden before the first check', async () => {
    const el = await render(null);
    expect(el.querySelector('section')).toBeNull();
  });

  it('shows the held tickers and why', async () => {
    const el = await render(GAPS);
    expect(el.textContent).toContain('Gaps, those buys held');
    expect(el.querySelector('.problem')?.textContent).toContain('B.US');
    const items = [...el.querySelectorAll('.items li')];
    expect(items.map((li) => li.textContent)).toEqual([
      expect.stringContaining('close 9.1% off'),
      expect.stringContaining('no price'),
    ]);
  });

  it('names the halt of a systematic gap', async () => {
    const el = await render(check({ status: 'systematic', halt_id: 4, held: ['A.US'] }));
    expect(el.textContent).toContain('Halt #4');
  });
});
