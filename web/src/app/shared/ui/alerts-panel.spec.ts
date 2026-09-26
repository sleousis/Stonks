import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { type ComponentFixture, TestBed } from '@angular/core/testing';

import type { AlertView } from '../../api/models';
import { provideApi } from '../../api/provide-api';
import { nextRequest, tick } from '../../../testing/http';
import { AlertsPanel, levelPill } from './alerts-panel';

const ALERT: AlertView = {
  id: 1,
  level: 'error',
  title: 'Price ingest failed',
  message: 'Two tickers had no data.',
  context: {},
  created_at: new Date(Date.now() - 5 * 60_000).toISOString(),
};

describe('AlertsPanel', () => {
  let fixture: ComponentFixture<AlertsPanel>;
  let http: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({ providers: [...provideApi(), provideHttpClientTesting()] });
    http = TestBed.inject(HttpTestingController);
    fixture = TestBed.createComponent(AlertsPanel);
    fixture.componentRef.setInput('limit', 2);
    fixture.detectChanges();
  });

  afterEach(() => http.verify());

  async function flush(body: object) {
    const req = await nextRequest(http, '/api/alerts');
    req.flush(body);
    await tick();
    fixture.detectChanges();
    return req;
  }

  it('lists alerts with the level as text and a relative time', async () => {
    const req = await flush({ items: [ALERT], total: 1, limit: 2, offset: 0 });
    expect(req.request.urlWithParams).toContain('limit=2');
    const el = fixture.nativeElement as HTMLElement;
    expect(el.textContent).toContain('Price ingest failed');
    expect(el.querySelector('app-status-pill')?.textContent?.trim()).toBe('Error');
    expect(el.querySelector('app-status-pill')?.getAttribute('data-tone')).toBe('negative');
    expect(el.querySelector('time')?.textContent?.trim()).toBe('5m ago');
    expect(el.querySelector('.pager')).toBeNull();
  });

  it('pages to older alerts', async () => {
    await flush({ items: [ALERT, { ...ALERT, id: 2 }], total: 3, limit: 2, offset: 0 });
    const el = fixture.nativeElement as HTMLElement;
    const older = [...el.querySelectorAll('button')].find((b) => b.textContent?.includes('Older'))!;
    older.click();
    fixture.detectChanges();
    const req = await flush({ items: [{ ...ALERT, id: 3 }], total: 3, limit: 2, offset: 2 });
    expect(req.request.urlWithParams).toContain('offset=2');
    expect(el.textContent).toContain('3 to 3 of 3');
  });

  it('explains what lands here when there are none', async () => {
    await flush({ items: [], total: 0, limit: 2, offset: 0 });
    expect((fixture.nativeElement as HTMLElement).textContent).toContain('No alerts');
  });
});

describe('levelPill', () => {
  it('maps levels to a tone and a word', () => {
    expect(levelPill('warning')).toEqual({ tone: 'warn', label: 'Warning' });
    expect(levelPill('info')).toEqual({ tone: 'info', label: 'Info' });
    expect(levelPill('odd').label).toBe('Odd');
  });
});
