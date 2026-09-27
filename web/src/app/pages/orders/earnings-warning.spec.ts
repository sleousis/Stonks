import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import { provideApi } from '../../api/provide-api';
import { ToastService } from '../../core/notify/toast.service';
import { nextRequest, tick } from '../../../testing/http';
import { EarningsWarningLine, warningText } from './earnings-warning';

const WARNING = {
  ticker: 'AAPL.US',
  name: 'Apple',
  report_date: '2026-10-29',
  before_after_market: 'after' as const,
  next_open: '2026-10-30T13:30:00Z',
};

describe('EarningsWarningLine', () => {
  let http: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [provideRouter([]), ...provideApi(), provideHttpClientTesting()],
    });
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  async function render(ticker: string) {
    const fixture = TestBed.createComponent(EarningsWarningLine);
    fixture.componentRef.setInput('ticker', ticker);
    fixture.detectChanges();
    return fixture;
  }

  it('warns when the ticker reports before the next open', async () => {
    const fixture = await render('aapl.us');
    const req = await nextRequest(http, '/api/calendars/earnings-warnings');
    expect(req.request.urlWithParams).toContain('tickers=AAPL.US');
    req.flush({ checked: ['AAPL.US'], warnings: [WARNING] });
    await tick();
    fixture.detectChanges();
    const el: HTMLElement = fixture.nativeElement;
    const line = el.querySelector('[role=status]')!;
    expect(line.textContent).toContain('Earnings before the next open.');
    expect(line.textContent).toContain('after the close');
    expect(el.querySelector('a')?.getAttribute('href')).toBe(
      '/calendar?ticker=AAPL.US&date=2026-10-29',
    );
  });

  it('shows nothing when there is no report', async () => {
    const fixture = await render('MSFT.US');
    (await nextRequest(http, '/api/calendars/earnings-warnings')).flush({
      checked: ['MSFT.US'],
      warnings: [],
    });
    await tick();
    fixture.detectChanges();
    expect(fixture.nativeElement.querySelector('[role=status]')).toBeNull();
  });

  it('shows nothing when the check fails', async () => {
    const fixture = await render('MSFT.US');
    (await nextRequest(http, '/api/calendars/earnings-warnings')).flush(
      { detail: 'boom' },
      { status: 500, statusText: 'Server Error' },
    );
    await tick();
    fixture.detectChanges();
    expect(fixture.nativeElement.querySelector('[role=status]')).toBeNull();
    // Silent: a failed check never toasts over the ticket.
    expect(TestBed.inject(ToastService).toasts()).toEqual([]);
  });

  it('does not ask about half-typed tickers', async () => {
    const fixture = await render('AAP');
    await tick(5);
    fixture.detectChanges();
    http.expectNone(() => true);
  });

  it('words the warning with the day and the next open', () => {
    const text = warningText({ ...WARNING, before_after_market: null });
    expect(text).toContain('AAPL.US reports earnings on 2026-10-29, before the next open');
    expect(text).not.toContain('time not given');
  });
});
