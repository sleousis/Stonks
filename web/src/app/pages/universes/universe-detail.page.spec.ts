import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { signal } from '@angular/core';
import { type ComponentFixture, TestBed } from '@angular/core/testing';
import { Router, provideRouter } from '@angular/router';

import type { MeView, UniverseView } from '../../api/models';
import { provideApi } from '../../api/provide-api';
import { SessionService } from '../../core/auth/session.service';
import { ConfirmService } from '../../core/confirm/confirm.service';
import { type JobHandle, JobsService } from '../../core/jobs/jobs.service';
import { isoDay } from '../../core/format/format';
import { ToastService } from '../../core/notify/toast.service';
import { ADMIN, TRADER } from '../../../testing/auth-fixtures';
import { nextRequest, tick } from '../../../testing/http';
import { UniverseDetailPage } from './universe-detail.page';

const UNIVERSE: UniverseView = {
  id: 'us-big',
  name: 'US large caps',
  description: 'Liquid names',
  kind: 'rule',
  spec: { rebalance: 'monthly', start: '2025-01-02' },
  member_count: 2,
  refreshed_at: '2026-09-25T06:00:00Z',
};

function finished(jobId: string): JobHandle {
  const event = { job_id: jobId, status: 'succeeded', progress: 1 } as const;
  return {
    jobId,
    event: signal(event),
    status: signal('succeeded'),
    progress: signal(1),
    message: signal(null),
    error: signal(null),
    done: signal(true),
    finished: Promise.resolve(event),
    stop: () => undefined,
  };
}

describe('UniverseDetailPage', () => {
  let fixture: ComponentFixture<UniverseDetailPage>;
  let http: HttpTestingController;
  let el: HTMLElement;
  let confirm: ReturnType<typeof vi.fn>;
  /** Who is signed in; a describe block can change it in beforeAll. */
  let me: MeView = ADMIN;

  async function settle(): Promise<void> {
    for (let i = 0; i < 4; i++) {
      await tick();
      fixture.detectChanges();
    }
  }

  function button(text: string): HTMLButtonElement | undefined {
    return [...el.querySelectorAll('button')].find((b) => b.textContent?.trim() === text);
  }

  const SPANS = [
    { ticker: 'OLD.US', start_date: '2020-01-02', end_date: '2024-03-01' },
    { ticker: 'AAPL.US', start_date: null, end_date: null },
  ];

  async function flushHistory(items = SPANS, total = items.length): Promise<void> {
    const req = await nextRequest(http, '/api/universes/us-big/history');
    req.flush({ items, total, limit: 50, offset: 0 });
  }

  async function flushMembers(tickers: string[], asOf = '2026-09-26'): Promise<void> {
    const req = await nextRequest(http, '/api/universes/us-big/members');
    req.flush({ universe_id: 'us-big', as_of: asOf, tickers, count: tickers.length });
  }

  beforeEach(async () => {
    confirm = vi.fn().mockResolvedValue(true);
    TestBed.configureTestingModule({
      providers: [
        ...provideApi(),
        provideHttpClientTesting(),
        provideRouter([]),
        { provide: ConfirmService, useValue: { confirm } },
        { provide: JobsService, useValue: { track: (id: string) => finished(id) } },
      ],
    });
    http = TestBed.inject(HttpTestingController);
    const signingIn = TestBed.inject(SessionService).load();
    (await nextRequest(http, '/api/auth/me')).flush(me);
    await signingIn;
    fixture = TestBed.createComponent(UniverseDetailPage);
    fixture.componentRef.setInput('id', 'us-big');
    el = fixture.nativeElement;
    fixture.detectChanges();
    (await nextRequest(http, '/api/universes/us-big')).flush(UNIVERSE);
    await flushMembers(['AAPL.US', 'MSFT.US']);
    await flushHistory();
    await settle();
  });

  afterEach(() => http.verify());

  it('shows the facts, the definition and members today', () => {
    expect(el.querySelector('h1')!.textContent).toContain('US large caps');
    expect(el.textContent).toContain('Rule');
    expect(el.textContent).toContain('"rebalance": "monthly"');
    const members = el.querySelector('[aria-labelledby="members-title"]')!;
    const rows = [...members.querySelectorAll('tbody tr')].map((tr) => tr.textContent?.trim());
    expect(rows).toEqual(['AAPL.US', 'MSFT.US']);
    expect(el.querySelector<HTMLInputElement>('#m-date')!.value).toBe(isoDay());
  });

  it('links to the lab with this universe picked (17.9)', () => {
    const link = [...el.querySelectorAll<HTMLAnchorElement>('a')].find(
      (a) => a.textContent?.trim() === 'Test in the lab',
    )!;
    expect(link.getAttribute('href')).toBe('/lab?universe=us-big');
  });

  it('5,000 members render one page', async () => {
    const tickers = Array.from({ length: 5000 }, (_, i) => `T${String(i).padStart(4, '0')}.US`);
    const date = el.querySelector<HTMLInputElement>('#m-date')!;
    date.value = '2024-01-02';
    date.dispatchEvent(new Event('change'));
    fixture.detectChanges();
    await flushMembers(tickers, '2024-01-02');
    await settle();
    const members = el.querySelector('[aria-labelledby="members-title"]')!;
    expect(members.querySelectorAll('tbody tr').length).toBeLessThanOrEqual(50);
    expect(members.textContent).toContain('of 5000');
    expect(members.querySelector('.chips')).toBeNull();

    const find = el.querySelector<HTMLInputElement>('#m-find')!;
    find.value = 't4999';
    find.dispatchEvent(new Event('input'));
    fixture.detectChanges();
    expect([...members.querySelectorAll('tbody tr')].map((tr) => tr.textContent?.trim())).toEqual([
      'T4999.US',
    ]);
  });

  it('result 500 after success shows inline error with retry (refresh)', async () => {
    button('Refresh')!.click();
    (await nextRequest(http, '/api/universes/us-big/refresh', 'POST')).flush({ id: 'job_r' });
    (await nextRequest(http, '/api/universes/refresh/job_r/result')).flush(
      { title: 'x', status: 500, detail: 'Result store unavailable.' },
      { status: 500, statusText: 'Server Error' },
    );
    // The membership changed either way, so the page reloads it.
    (await nextRequest(http, '/api/universes/us-big')).flush(UNIVERSE);
    await flushMembers(['AAPL.US', 'MSFT.US']);
    await flushHistory();
    await settle();
    const error = el.querySelector('app-job-progress app-error-state')!;
    expect(error.textContent).toContain('Refresh finished, but its result could not load');
    error.querySelector('button')!.click();
    (await nextRequest(http, '/api/universes/refresh/job_r/result')).flush({
      universe_id: 'us-big',
      kind: 'rule',
      members: 5,
      current_members: 3,
      spans: 6,
      warnings: [],
    });
    await settle();
    expect(el.querySelector('[aria-label="Refresh result"]')).not.toBeNull();
    expect(el.querySelector('app-job-progress app-error-state')).toBeNull();
  });

  it('result 500 after success shows inline error with retry (fetch missing data)', async () => {
    button('Fetch missing data')!.click();
    (await nextRequest(http, '/api/universes/us-big/ensure', 'POST')).flush({ id: 'job_e' });
    (await nextRequest(http, '/api/universes/ensure/job_e/result')).flush(
      { title: 'x', status: 500, detail: 'Result store unavailable.' },
      { status: 500, statusText: 'Server Error' },
    );
    await settle();
    const error = el.querySelector('app-job-progress app-error-state')!;
    expect(error.textContent).toContain(
      'Fetch missing data finished, but its result could not load',
    );
    error.querySelector('button')!.click();
    (await nextRequest(http, '/api/universes/ensure/job_e/result')).flush({
      interval: '1d',
      start: '2025-01-01',
      end: '2025-06-30',
      source: 'eodhd',
      tickers_requested: 3,
      tickers_fetched: 2,
      tickers_up_to_date: 1,
      tickers_failed: 0,
      warnings: [],
      failed: [],
    });
    await settle();
    expect(el.querySelector('[aria-label="Fetch missing data result"]')).not.toBeNull();
  });

  it('reads members on another date', async () => {
    const date = el.querySelector<HTMLInputElement>('#m-date')!;
    date.value = '2020-01-02';
    date.dispatchEvent(new Event('change'));
    fixture.detectChanges();
    const req = await nextRequest(http, '/api/universes/us-big/members');
    expect(req.request.urlWithParams).toContain('as_of=2020-01-02');
    req.flush({ universe_id: 'us-big', as_of: '2020-01-02', tickers: [], count: 0 });
    await settle();
    expect(el.textContent).toContain('No members on this date');
  });

  it('refreshes, follows the job and shows the counts and warnings', async () => {
    const success = vi.spyOn(TestBed.inject(ToastService), 'success');
    button('Refresh')!.click();
    (await nextRequest(http, '/api/universes/us-big/refresh', 'POST')).flush({ id: 'job_r' });
    (await nextRequest(http, '/api/universes/refresh/job_r/result')).flush({
      universe_id: 'us-big',
      kind: 'rule',
      members: 5,
      current_members: 3,
      spans: 6,
      warnings: ['OLD.US has no dates'],
    });
    (await nextRequest(http, '/api/universes/us-big')).flush({ ...UNIVERSE, member_count: 5 });
    await flushMembers(['AAPL.US', 'MSFT.US', 'NVDA.US']);
    await flushHistory();
    await settle();
    expect(success).toHaveBeenCalledWith('Refreshed US large caps: 3 members today.');
    const result = el.querySelector('[aria-label="Refresh result"]')!;
    expect(result.textContent).toContain('6');
    expect(el.textContent).toContain('OLD.US has no dates');
    expect(el.querySelector('app-job-progress')!.textContent).toContain('Finished.');
  });

  it('ensures data over the window and reports what it fetched', async () => {
    const start = el.querySelector<HTMLInputElement>('#e-start')!;
    start.value = '2025-01-01';
    start.dispatchEvent(new Event('change'));
    const end = el.querySelector<HTMLInputElement>('#e-end')!;
    end.value = '2025-06-30';
    end.dispatchEvent(new Event('change'));
    fixture.detectChanges();

    expect(confirm).not.toHaveBeenCalled();
    button('Fetch missing data')!.click();
    const post = await nextRequest(http, '/api/universes/us-big/ensure', 'POST');
    expect(post.request.body).toEqual({
      start: '2025-01-01',
      end: '2025-06-30',
      interval: '1d',
      source: null,
    });
    post.flush({ id: 'job_e' });
    (await nextRequest(http, '/api/universes/ensure/job_e/result')).flush({
      interval: '1d',
      start: '2025-01-01',
      end: '2025-06-30',
      source: 'eodhd',
      tickers_requested: 3,
      tickers_fetched: 2,
      tickers_up_to_date: 1,
      tickers_failed: 0,
      warnings: [],
      failed: [],
    });
    await settle();
    expect(confirm).toHaveBeenCalledWith(
      expect.objectContaining({ confirmLabel: 'Fetch missing data' }),
    );
    const result = el.querySelector('[aria-label="Fetch missing data result"]')!;
    expect(result.textContent).toContain('Fetched');
    expect(result.textContent).toContain('2025-01-01 to 2025-06-30');
  });

  it('refuses an inverted window', async () => {
    const start = el.querySelector<HTMLInputElement>('#e-start')!;
    start.value = '2026-12-01';
    start.dispatchEvent(new Event('change'));
    fixture.detectChanges();
    button('Fetch missing data')!.click();
    await settle();
    expect(el.textContent).toContain('start date must be on or before');
    expect(confirm).not.toHaveBeenCalled();
  });

  it('shows who joined and left, finds a ticker and pages on', async () => {
    const history = el.querySelector('[aria-labelledby="history-title"]')!;
    const rows = [...history.querySelectorAll('tbody tr')].map((tr) => tr.textContent);
    expect(rows[0]).toContain('OLD.US');
    expect(rows[0]).toContain('2024-03-01');
    expect(rows[1]).toContain('From the start');
    expect(rows[1]).toContain('Still a member');
    expect(history.textContent).toContain('2 spans');

    const find = el.querySelector<HTMLInputElement>('#h-find')!;
    find.value = 'old';
    find.dispatchEvent(new Event('change'));
    fixture.detectChanges();
    const req = await nextRequest(http, '/api/universes/us-big/history');
    expect(req.request.urlWithParams).toContain('ticker=old');
    req.flush({ items: [SPANS[0]], total: 120, limit: 50, offset: 0 });
    await settle();
    button('Show more (1 of 120)')!.click();
    const more = await nextRequest(http, '/api/universes/us-big/history');
    expect(more.request.urlWithParams).toContain('limit=100');
    more.flush({ items: SPANS, total: 120, limit: 100, offset: 0 });
    await settle();
  });

  it('edits the definition in place and says to refresh', async () => {
    const success = vi.spyOn(TestBed.inject(ToastService), 'success');
    button('Edit')!.click();
    fixture.detectChanges();
    (await nextRequest(http, '/api/universes')).flush({
      items: [UNIVERSE],
      total: 1,
      limit: 500,
      offset: 0,
    });
    (await nextRequest(http, '/api/screener/metrics')).flush([]);
    await settle();
    const panel = el.querySelector('#edit-panel')!;
    expect(panel.querySelector<HTMLInputElement>('#u-id')!.value).toBe('us-big');
    button('Save changes')!.click();
    const put = await nextRequest(http, '/api/universes/us-big', 'PUT');
    expect(put.request.body).toMatchObject({ kind: 'rule', name: 'US large caps' });
    put.flush({ ...UNIVERSE, updated_at: '2026-09-26T08:00:00Z' });
    await settle();
    expect(el.querySelector('#edit-panel')).toBeNull();
    expect(success).toHaveBeenCalledWith('Saved US large caps. Refresh it to rebuild the members.');
    // Changed after the last refresh: the page says the members are behind.
    expect(el.querySelector('.notice')?.textContent).toContain('changed after the last refresh');
  });

  it('deletes after typing the id, then goes back to the list', async () => {
    const navigate = vi.spyOn(TestBed.inject(Router), 'navigate').mockResolvedValue(true);
    button('Delete')!.click();
    const del = await nextRequest(http, '/api/universes/us-big', 'DELETE');
    expect(confirm).toHaveBeenCalledWith(
      expect.objectContaining({ typedConfirmation: 'us-big', tone: 'danger' }),
    );
    del.flush(UNIVERSE);
    await settle();
    expect(navigate).toHaveBeenCalledWith(['/universes']);
  });

  describe('as a trader', () => {
    beforeAll(() => (me = TRADER));
    afterAll(() => (me = ADMIN));

    it('can refresh and fetch data, but only admins delete', () => {
      expect(button('Refresh')!.disabled).toBe(false);
      expect(button('Fetch missing data')!.disabled).toBe(false);
      expect(button('Delete')!.disabled).toBe(true);
      expect(button('Edit')!.disabled).toBe(false);
      expect(el.querySelector('app-page-header')!.textContent).toContain('Admins only.');
      expect(el.textContent).not.toContain('the lake');
    });
  });
});
