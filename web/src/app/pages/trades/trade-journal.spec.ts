import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { type ComponentFixture, TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import { provideApi } from '../../api/provide-api';
import { nextRequest, tick } from '../../../testing/http';
import { entry } from './tca.fixtures';
import { TradeJournal } from './trade-journal';

describe('TradeJournal', () => {
  let fixture: ComponentFixture<TradeJournal>;
  let controller: HttpTestingController;
  let el: HTMLElement;

  beforeEach(() => {
    TestBed.configureTestingModule({
      imports: [TradeJournal],
      providers: [...provideApi(), provideHttpClientTesting(), provideRouter([])],
    });
    controller = TestBed.inject(HttpTestingController);
    fixture = TestBed.createComponent(TradeJournal);
    el = fixture.nativeElement;
    fixture.detectChanges();
  });

  afterEach(() => controller.verify());

  async function settle(): Promise<void> {
    for (let i = 0; i < 4; i++) {
      await tick(5);
      fixture.detectChanges();
    }
  }

  it('lists orders with side, figures and notes, each linking to its detail', async () => {
    const req = await nextRequest(controller, '/api/tca/journal');
    expect(req.request.urlWithParams).toContain('limit=25');
    expect(req.request.urlWithParams).toContain('offset=0');
    req.flush({
      items: [entry({ notes: [{ id: 1 } as never, { id: 2 } as never] })],
      total: 1,
      limit: 25,
      offset: 0,
    });
    await settle();

    const link = el.querySelector<HTMLAnchorElement>('table a')!;
    expect(link.textContent?.trim()).toBe('AAPL.US');
    expect(link.getAttribute('href')).toBe('/trades/orders/2026-09-25:momentum-v3:AAPL.US:buy');
    const row = el.querySelector('tbody tr')!;
    expect(row.querySelector('app-side-tag')?.textContent).toContain('Buy');
    expect(row.textContent).toContain('$200.00');
    expect(row.textContent).toContain('$200.30');
    expect(row.textContent).toContain('17');
    // The raw client id never shows as text.
    expect(el.textContent).not.toContain('momentum-v3:AAPL');
  });

  it('keeps the rows, dimmed, and focus on Next while the next page loads (UX-35)', async () => {
    const rows = (offset: number) =>
      Array.from({ length: 25 }, (_, i) =>
        entry({ client_id: `c${offset + i}`, ticker: `T${offset + i}.US` }),
      );
    (await nextRequest(controller, '/api/tca/journal')).flush({
      items: rows(0),
      total: 60,
      limit: 25,
      offset: 0,
    });
    await settle();
    const next = [...el.querySelectorAll<HTMLButtonElement>('.pager button')].find(
      (b) => b.textContent?.trim() === 'Next',
    )!;
    next.focus();
    next.click();
    fixture.detectChanges();
    const req = await nextRequest(controller, '/api/tca/journal');
    expect(req.request.urlWithParams).toContain('offset=25');
    fixture.detectChanges();
    expect(el.querySelectorAll('tbody tr').length).toBe(25);
    expect(el.querySelector('app-loading-state')).toBeNull();
    expect(el.querySelector('.table-wrap')?.getAttribute('aria-busy')).toBe('true');
    expect(document.activeElement).toBe(next);
    req.flush({ items: rows(25), total: 60, limit: 25, offset: 25 });
    await settle();
    expect(el.querySelector('tbody tr')?.textContent).toContain('T25.US');
    expect(document.activeElement).toBe(next);
  });

  it('shows a calm empty state', async () => {
    (await nextRequest(controller, '/api/tca/journal')).flush({
      items: [],
      total: 0,
      limit: 25,
      offset: 0,
    });
    await settle();
    expect(el.textContent).toContain('No trades yet');
    expect(el.textContent).toContain('Costs appear after the first filled order.');
  });
});
