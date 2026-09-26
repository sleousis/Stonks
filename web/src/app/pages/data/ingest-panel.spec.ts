import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { type ComponentFixture, TestBed } from '@angular/core/testing';

import type { DataSourceInfo, Job } from '../../api/models';
import { provideApi } from '../../api/provide-api';
import { SessionService } from '../../core/auth/session.service';
import { ConfirmService } from '../../core/confirm/confirm.service';
import { JOB_FETCH, JOB_POLL_MS } from '../../core/jobs/jobs.service';
import { ToastService } from '../../core/notify/toast.service';
import { ADMIN, TRADER } from '../../../testing/auth-fixtures';
import { nextRequest, tick } from '../../../testing/http';
import { IngestPanel } from './ingest-panel';

const SOURCES: DataSourceInfo[] = [
  { id: 'eodhd', configured: true, default: true, detail: null },
  { id: 'yahoo', configured: true, default: false, detail: null },
  // Listed by the API; the generated union may not know it yet.
  {
    id: 'defillama' as DataSourceInfo['id'],
    configured: false,
    default: false,
    detail: 'crypto TVL only',
  },
];

function job(status: Job['status'], progress = 0): Job {
  return {
    id: 'job_1',
    kind: 'ingest',
    status,
    progress,
    params: {},
    created_at: '2026-09-26T12:00:00Z',
    message: status === 'running' ? 'AAPL.US' : null,
  };
}

describe('IngestPanel', () => {
  let fixture: ComponentFixture<IngestPanel>;
  let http: HttpTestingController;
  let confirm: ConfirmService;
  let el: HTMLElement;

  function set(id: string, value: string): void {
    const field = el.querySelector<HTMLInputElement | HTMLSelectElement>(`#${id}`)!;
    field.value = value;
    field.dispatchEvent(new Event(field instanceof HTMLSelectElement ? 'change' : 'input'));
    fixture.detectChanges();
  }

  function submit(): void {
    el.querySelector('form')!.dispatchEvent(new Event('submit'));
    fixture.detectChanges();
  }

  async function setup(me = ADMIN): Promise<void> {
    TestBed.configureTestingModule({
      providers: [
        ...provideApi(),
        provideHttpClientTesting(),
        // No event stream in tests: the jobs service falls back to polling.
        { provide: JOB_FETCH, useValue: () => Promise.reject(new Error('no stream')) },
        { provide: JOB_POLL_MS, useValue: 1 },
      ],
    });
    http = TestBed.inject(HttpTestingController);
    confirm = TestBed.inject(ConfirmService);
    const loading = TestBed.inject(SessionService).load();
    (await nextRequest(http, '/api/auth/me')).flush(me);
    await loading;
    fixture = TestBed.createComponent(IngestPanel);
    el = fixture.nativeElement;
    fixture.detectChanges();
    (await nextRequest(http, '/api/sources')).flush(SOURCES);
    await tick();
    fixture.detectChanges();
  }

  beforeEach(async () => {
    if (!expect.getState().currentTestName?.includes('trader')) await setup();
  });

  /** Fill the form, confirm, and bring the job to `succeeded`. */
  async function runToSuccess(): Promise<void> {
    set('ingest-tickers', 'aapl.us');
    submit();
    await tick();
    confirm.request()!.resolve(true);
    (await nextRequest(http, '/api/ingest/runs', 'POST')).flush(job('queued'), {
      status: 202,
      statusText: 'Accepted',
    });
    (await nextRequest(http, '/api/jobs/job_1')).flush(job('succeeded', 1));
  }

  it('a trader sees why Run ingest is off and nothing is asked', async () => {
    await setup(TRADER);
    const button = el.querySelector<HTMLButtonElement>('button[type=submit]')!;
    expect(button.disabled).toBe(true);
    expect(el.querySelector('.permission-note')?.textContent).toContain('Admins only.');
    expect(el.textContent).not.toContain('API token');
    set('ingest-tickers', 'aapl.us');
    submit();
    await tick();
    expect(confirm.request()).toBeNull();
  });

  it('result 500 after success shows an inline error with retry', async () => {
    await runToSuccess();
    (await nextRequest(http, '/api/ingest/jobs/job_1/result')).flush(
      { title: 'x', status: 500, detail: 'Result store unavailable.' },
      { status: 500, statusText: 'Server Error' },
    );
    await tick();
    fixture.detectChanges();
    const error = el.querySelector('app-error-state')!;
    expect(error.textContent).toContain('its result could not load');
    error.querySelector<HTMLButtonElement>('button')!.click();
    (await nextRequest(http, '/api/ingest/jobs/job_1/result')).flush({
      run_id: 7,
      kind: 'prices',
      status: 'ok',
      tickers_ok: 1,
      tickers_failed: 0,
    });
    await tick();
    fixture.detectChanges();
    expect(el.querySelector('app-error-state')).toBeNull();
    expect(el.textContent).toContain('Run #7 ok');
  });

  it('cancel asks first ("Cancel job" / "Keep running") and only then cancels', async () => {
    set('ingest-tickers', 'aapl.us');
    submit();
    await tick();
    confirm.request()!.resolve(true);
    (await nextRequest(http, '/api/ingest/runs', 'POST')).flush(job('queued'), {
      status: 202,
      statusText: 'Accepted',
    });
    (await nextRequest(http, '/api/jobs/job_1')).flush(job('queued'));
    await tick(5);
    fixture.detectChanges();
    expect(el.textContent).not.toContain('job_1');

    const cancel = () =>
      [...el.querySelectorAll<HTMLButtonElement>('.job button')].find(
        (b) => b.textContent?.trim() === 'Cancel',
      )!;
    cancel().click();
    await tick();
    const ask = confirm.request()!;
    expect(ask.confirmLabel).toBe('Cancel job');
    expect(ask.cancelLabel).toBe('Keep running');
    ask.resolve(false);
    await tick();
    expect(http.match((r) => r.method === 'POST' && r.url.includes('cancel')).length).toBe(0);

    cancel().click();
    await tick();
    confirm.request()!.resolve(true);
    const post = await nextRequest(http, '/api/jobs/job_1/cancel', 'POST');
    post.flush(job('cancelled'));
    // Let polling see the cancelled job so the handle settles.
    for (const r of http.match((req) => req.url.endsWith('/api/jobs/job_1'))) {
      r.flush(job('cancelled'));
    }
  });

  it('offers every source the API lists and flags unconfigured ones', () => {
    const options = [...el.querySelectorAll<HTMLOptionElement>('#ingest-source option')];
    expect(options.map((o) => o.value)).toEqual(['eodhd', 'yahoo', 'defillama']);
    expect(options[2].textContent).toContain('not configured');
  });

  it('shows problems and asks nothing when the form is incomplete', async () => {
    submit();
    await tick();
    expect(el.textContent).toContain('Enter at least one ticker, or an exchange.');
    expect(confirm.request()).toBeNull();
    http.verify();
  });

  it('asks before starting and sends nothing when cancelled', async () => {
    set('ingest-tickers', 'aapl.us');
    submit();
    await tick();
    const request = confirm.request()!;
    expect(request.title).toBe('Run daily prices ingest?');
    expect(request.confirmLabel).toBe('Run ingest');
    request.resolve(false);
    await tick();
    http.verify();
  });

  it('starts the ingest after confirming and follows the job to its result', async () => {
    const toasts = TestBed.inject(ToastService);
    set('ingest-source', 'yahoo');
    set('ingest-tickers', 'aapl.us msft.us');
    set('ingest-since', '2026-01-02');
    submit();
    await tick();

    const request = confirm.request()!;
    expect(request.message).toBe(
      'Fetches prices for 2 tickers (AAPL.US, MSFT.US) from yahoo since 2026-01-02 and saves them.',
    );
    request.resolve(true);

    const post = await nextRequest(http, '/api/ingest/runs', 'POST');
    expect(post.request.body).toEqual({
      source: 'yahoo',
      kind: 'prices',
      tickers: ['AAPL.US', 'MSFT.US'],
      since: '2026-01-02',
    });
    post.flush(job('queued'), { status: 202, statusText: 'Accepted' });
    await tick();
    fixture.detectChanges();
    expect(el.querySelector<HTMLButtonElement>('button[type=submit]')!.disabled).toBe(true);

    (await nextRequest(http, '/api/jobs/job_1')).flush(job('running', 0.5));
    await tick(5);
    fixture.detectChanges();
    expect(el.querySelector('progress')!.value).toBe(0.5);
    expect(el.textContent).toContain('50%');

    (await nextRequest(http, '/api/jobs/job_1')).flush(job('succeeded', 1));
    (await nextRequest(http, '/api/ingest/jobs/job_1/result')).flush({
      run_id: 42,
      kind: 'prices',
      status: 'ok',
      tickers_ok: 2,
      tickers_failed: 0,
    });
    await tick();
    fixture.detectChanges();

    expect(el.textContent).toContain('Run #42 ok: 2 ok, 0 failed.');
    expect(toasts.toasts().some((t) => t.message.includes('#42'))).toBe(true);
    expect(el.querySelector<HTMLButtonElement>('button[type=submit]')!.disabled).toBe(false);
  });

  it('sends the interval for intraday and asks for one first', async () => {
    set('ingest-kind', 'intraday');
    set('ingest-tickers', 'AAPL.US');
    submit();
    await tick();
    expect(el.textContent).toContain('Choose an interval for intraday bars.');

    set('ingest-interval', '5m');
    submit();
    await tick();
    confirm.request()!.resolve(true);
    const post = await nextRequest(http, '/api/ingest/runs', 'POST');
    expect(post.request.body).toMatchObject({ kind: 'intraday', interval: '5m' });
    post.flush(job('failed'), { status: 202, statusText: 'Accepted' });
    (await nextRequest(http, '/api/jobs/job_1')).flush({ ...job('failed'), error: 'vendor down' });
    await tick();
    fixture.detectChanges();
    expect(el.textContent).toContain('vendor down');
  });
});
