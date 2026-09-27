import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import { provideApi } from '../../api/provide-api';
import { ToastService } from '../../core/notify/toast.service';
import { nextRequest, tick } from '../../../testing/http';
import { EventAlertKinds } from './event-alert-kinds';

describe('EventAlertKinds', () => {
  let http: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [provideRouter([]), ...provideApi(), provideHttpClientTesting()],
    });
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  it('lists each kind with how far ahead it looks', async () => {
    const fixture = TestBed.createComponent(EventAlertKinds);
    fixture.detectChanges();
    (await nextRequest(http, '/api/calendars/alert-kinds')).flush([
      { kind: 'earnings_upcoming', label: 'Earnings coming up', default_days_ahead: 2 },
      { kind: 'ex_dividend_upcoming', label: 'Ex-dividend coming up', default_days_ahead: 1 },
    ]);
    await tick();
    fixture.detectChanges();
    const items = [...fixture.nativeElement.querySelectorAll('li')].map((li: Element) =>
      [...li.querySelectorAll('span')].map((s) => s.textContent?.trim()).join(' | '),
    );
    expect(items).toEqual([
      'Earnings coming up | 2 days ahead',
      'Ex-dividend coming up | A day ahead',
    ]);
    expect(fixture.nativeElement.textContent).toContain('They come as Signals');
  });

  it('shows nothing when the list cannot be read', async () => {
    const fixture = TestBed.createComponent(EventAlertKinds);
    fixture.detectChanges();
    const req = await nextRequest(http, '/api/calendars/alert-kinds');
    req.flush({ detail: 'no' }, { status: 500, statusText: 'Server Error' });
    await tick();
    fixture.detectChanges();
    expect(fixture.nativeElement.querySelector('section')).toBeNull();
    expect(TestBed.inject(ToastService).toasts()).toEqual([]);
  });
});
