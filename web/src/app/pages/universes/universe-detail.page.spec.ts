import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { signal } from '@angular/core';
import { type ComponentFixture, TestBed } from '@angular/core/testing';
import { Router, provideRouter } from '@angular/router';

import type { UniverseView } from '../../api/models';
import { provideApi } from '../../api/provide-api';
import { ConfirmService } from '../../core/confirm/confirm.service';
import { type JobHandle, JobsService } from '../../core/jobs/jobs.service';
import { ToastService } from '../../core/notify/toast.service';
import { nextRequest, tick } from '../../../testing/http';
import { UniverseDetailPage } from './universe-detail.page';

const UNIVERSE: UniverseView = {
  id: 'us-big',
  name: 'US large caps',
  description: 'Liquid names',
  kind: 'rule',
  spec: { rebalance: 'monthly' },
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

  async function settle(): Promise<void> {
    for (let i = 0; i < 4; i++) {
      await tick();
      fixture.detectChanges();
    }
  }

  function button(text: string): HTMLButtonElement | undefined {
    return [...el.querySelectorAll('button')].find((b) => b.textContent?.trim() === text);
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
    fixture = TestBed.createComponent(UniverseDetailPage);
    fixture.componentRef.setInput('id', 'us-big');
    el = fixture.nativeElement;
    fixture.detectChanges();
    (await nextRequest(http, '/api/universes/us-big')).flush(UNIVERSE);
    await flushMembers(['AAPL.US', 'MSFT.US']);
    await settle();
  });

  afterEach(() => http.verify());

  it('shows the facts, the definition and members today', () => {
    expect(el.querySelector('h1')!.textContent).toContain('US large caps');
    expect(el.textContent).toContain('Rule');
    expect(el.textContent).toContain('"rebalance": "monthly"');
    const chips = [...el.querySelectorAll('.chips li')].map((li) => li.textContent?.trim());
    expect(chips).toEqual(['AAPL.US', 'MSFT.US']);
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
    await settle();
    expect(success).toHaveBeenCalledWith('Refreshed us-big: 3 members today.');
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

    button('Ensure data')!.click();
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
    const result = el.querySelector('[aria-label="Ensure data result"]')!;
    expect(result.textContent).toContain('Fetched');
    expect(result.textContent).toContain('2025-01-01 to 2025-06-30');
  });

  it('refuses an inverted window', async () => {
    const start = el.querySelector<HTMLInputElement>('#e-start')!;
    start.value = '2026-12-01';
    start.dispatchEvent(new Event('change'));
    fixture.detectChanges();
    button('Ensure data')!.click();
    await settle();
    expect(el.textContent).toContain('start date must be on or before');
    expect(confirm).not.toHaveBeenCalled();
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
});
