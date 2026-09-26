import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { type ComponentFixture, TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import type { Page, StatementFlagView } from '../../api/models';
import { provideApi } from '../../api/provide-api';
import { nextRequest, tick } from '../../../testing/http';
import { DataQualityPage } from './data-quality.page';

const FLAGS: Page<StatementFlagView> = {
  items: [
    {
      ticker: 'AAPL.US',
      period_end: '2025-12-31',
      frequency: 'quarterly',
      check_id: 'balance_identity',
      severity: 'error',
      detail: 'assets differ from liabilities plus equity by 12%',
      flagged_at: '2026-09-25T06:00:00Z',
    },
  ],
  total: 1,
  limit: 50,
  offset: 0,
};

describe('DataQualityPage', () => {
  let fixture: ComponentFixture<DataQualityPage>;
  let http: HttpTestingController;
  let el: HTMLElement;

  async function settle(): Promise<void> {
    await tick();
    fixture.detectChanges();
    await tick();
    fixture.detectChanges();
  }

  beforeEach(async () => {
    TestBed.configureTestingModule({
      providers: [...provideApi(), provideHttpClientTesting(), provideRouter([])],
    });
    http = TestBed.inject(HttpTestingController);
    fixture = TestBed.createComponent(DataQualityPage);
    el = fixture.nativeElement;
    fixture.detectChanges();
  });

  afterEach(() => http.verify());

  it('lists flags with the ticker linked to the data page', async () => {
    const req = await nextRequest(http, '/api/statements/flags');
    expect(req.request.urlWithParams).toContain('limit=50');
    req.flush(FLAGS);
    await settle();
    expect(el.textContent).toContain('1 flagged');
    expect(el.textContent).toContain('Balance identity');
    expect(el.textContent).toContain('assets differ');
    const link = el.querySelector<HTMLAnchorElement>('app-data-table a')!;
    expect(link.getAttribute('href')).toBe('/data?instrument=AAPL.US');
  });

  it('filters by ticker and severity and says when nothing matches', async () => {
    (await nextRequest(http, '/api/statements/flags')).flush(FLAGS);
    await settle();

    const input = el.querySelector<HTMLInputElement>('#flag-ticker')!;
    input.value = 'msft.us';
    el.querySelector<HTMLFormElement>('form')!.dispatchEvent(new Event('submit'));
    fixture.detectChanges();
    const byTicker = await nextRequest(http, '/api/statements/flags');
    expect(byTicker.request.urlWithParams).toContain('ticker=MSFT.US');
    byTicker.flush({ ...FLAGS, items: [], total: 0 });
    await settle();
    expect(el.textContent).toContain('No flags match');

    const select = el.querySelector<HTMLSelectElement>('#flag-severity')!;
    select.value = 'warning';
    select.dispatchEvent(new Event('change'));
    fixture.detectChanges();
    const bySeverity = await nextRequest(http, '/api/statements/flags');
    expect(bySeverity.request.urlWithParams).toContain('severity=warning');
    bySeverity.flush({ ...FLAGS, items: [], total: 0 });
    await settle();
  });

  it('explains an empty list in plain words', async () => {
    (await nextRequest(http, '/api/statements/flags')).flush({ ...FLAGS, items: [], total: 0 });
    await settle();
    expect(el.textContent).toContain('No statement flags');
    expect(el.textContent).toContain('checked each time their figures are updated');
    expect(el.textContent).not.toContain('stonks');
  });
});
