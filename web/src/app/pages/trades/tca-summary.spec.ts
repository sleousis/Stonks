import {
  HttpTestingController,
  type TestRequest,
  provideHttpClientTesting,
} from '@angular/common/http/testing';
import { type ComponentFixture, TestBed } from '@angular/core/testing';

import { provideApi } from '../../api/provide-api';
import { nextRequest, tick } from '../../../testing/http';
import { group, summary } from './tca.fixtures';
import { TcaSummary } from './tca-summary';

describe('TcaSummary', () => {
  let fixture: ComponentFixture<TcaSummary>;
  let controller: HttpTestingController;
  let el: HTMLElement;

  beforeEach(() => {
    TestBed.configureTestingModule({
      imports: [TcaSummary],
      providers: [...provideApi(), provideHttpClientTesting()],
    });
    controller = TestBed.inject(HttpTestingController);
    fixture = TestBed.createComponent(TcaSummary);
    el = fixture.nativeElement;
    fixture.detectChanges();
  });

  afterEach(() => controller.verify());

  function byOf(r: TestRequest): string | null {
    return new URL(r.request.urlWithParams, 'http://x').searchParams.get('by');
  }

  async function settle(): Promise<void> {
    for (let i = 0; i < 4; i++) {
      await tick(5);
      fixture.detectChanges();
    }
  }

  /** Wait until the totals and the breakdown reads are both pending. */
  async function both(): Promise<TestRequest[]> {
    const found: TestRequest[] = [];
    for (let i = 0; i < 500 && found.length < 2; i++) {
      await tick(1);
      found.push(...controller.match((r) => r.url.split('?')[0] === '/api/tca/summary'));
    }
    expect(found.length).toBe(2);
    return found;
  }

  /** The two summary reads go out together; tell them apart by `by`. */
  async function flushSummaries(breakdownBy: string, groups = [group({ key: 'momentum-v3' })]) {
    const reqs = await both();
    for (const r of reqs) {
      const by = byOf(r);
      if (by === 'all') r.flush(summary());
      else {
        expect(by).toBe(breakdownBy);
        r.flush(summary({ by: by as 'strategy', groups }));
      }
    }
    await settle();
  }

  it('shows the headline figures and explains shortfall', async () => {
    await flushSummaries('strategy');
    const text = el.textContent ?? '';
    expect(text).toContain('Shortfall is the gap between the price when the order was decided');
    expect(text).toContain('+12.3 bps');
    expect(text).toContain('3 of 4');
    expect(text).toContain('75% filled');
    expect(text).toContain('$14.84'); // 12.34 shortfall + 2.50 missed fills
    expect(el.querySelector('table')?.textContent).toContain('momentum-v3');
  });

  it('shows money in the portfolio base currency, converted by the server', async () => {
    const base = {
      base_currency: 'EUR',
      groups_base: [{ key: 'all', filled_notional: 9000, is_cost: 10, opportunity_cost: 2 }],
    };
    const eur = { key: 'momentum-v3', filled_notional: 9000, is_cost: 10, opportunity_cost: 2 };
    for (const r of await both()) {
      const by = byOf(r);
      if (by === 'all') r.flush(summary(base));
      else
        r.flush(
          summary({
            by: 'strategy',
            groups: [group({ key: 'momentum-v3' })],
            base_currency: 'EUR',
            groups_base: [eur],
          }),
        );
    }
    await settle();
    const text = el.textContent ?? '';
    expect(text).toContain('€12.00');
    expect(text).not.toContain('$');
    expect(el.querySelector('table')?.textContent).toContain('€10.00');
  });

  it('asks for another grouping when the switch changes', async () => {
    await flushSummaries('strategy');
    const byTicker = Array.from(el.querySelectorAll<HTMLButtonElement>('[role=radio]')).find(
      (b) => b.textContent?.trim() === 'By ticker',
    )!;
    byTicker.click();
    fixture.detectChanges();
    const req = await nextRequest(controller, '/api/tca/summary');
    expect(byOf(req)).toBe('ticker');
    req.flush(summary({ by: 'ticker', groups: [group({ key: 'AAPL.US' })] }));
    await settle();
    const table = el.querySelector('table')!;
    expect(table.textContent).toContain('Ticker');
    expect(table.textContent).toContain('AAPL.US');
  });

  it('shows a calm empty state before the first order', async () => {
    for (const r of await both()) {
      r.flush(summary({ groups: [] }));
    }
    await settle();
    expect(el.textContent).toContain('Costs appear after the first filled order.');
    expect(el.querySelector('table')).toBeNull();
  });
});
