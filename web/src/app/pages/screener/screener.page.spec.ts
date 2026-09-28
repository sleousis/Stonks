import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import type { ComponentFixture } from '@angular/core/testing';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import { provideApi } from '../../api/provide-api';
import { SessionService } from '../../core/auth/session.service';
import { JobsService } from '../../core/jobs/jobs.service';
import { ConfirmService } from '../../core/confirm/confirm.service';
import { ToastService } from '../../core/notify/toast.service';
import { TRADER, problem } from '../../../testing/auth-fixtures';
import { nextRequest, page, tick } from '../../../testing/http';
import {
  METRICS,
  RESULT,
  SAVED,
  controlledJob,
  screenSize,
} from '../../../testing/screener-fixtures';
import { ScreenerPage } from './screener.page';
import { provideFakeDataCoverage } from '../../../testing/fake-data-coverage';

describe('ScreenerPage', () => {
  let http: HttpTestingController;
  const confirm = vi.fn();
  let job = controlledJob('job_1');
  const track = vi.fn(() => job.handle);

  beforeEach(async () => {
    confirm.mockReset();
    job = controlledJob('job_1');
    track.mockClear();
    TestBed.configureTestingModule({
      providers: [
        provideFakeDataCoverage(),
        provideRouter([]),
        ...provideApi(),
        provideHttpClientTesting(),
        { provide: ConfirmService, useValue: { confirm } },
        { provide: JobsService, useValue: { track } },
      ],
    });
    http = TestBed.inject(HttpTestingController);
    const session = TestBed.inject(SessionService);
    const loading = session.load();
    (await nextRequest(http, '/api/auth/me')).flush(TRADER);
    await loading;
  });

  afterEach(() => http.verify());

  async function settle(fixture: ComponentFixture<unknown>) {
    await tick();
    fixture.detectChanges();
    await tick();
    fixture.detectChanges();
  }

  async function render(screens = [SAVED]) {
    const fixture = TestBed.createComponent(ScreenerPage);
    fixture.detectChanges();
    (await nextRequest(http, '/api/screener/metrics')).flush(METRICS);
    (await nextRequest(http, '/api/screener/screens')).flush(page(screens));
    (await nextRequest(http, '/api/universes')).flush(
      page([{ id: 'sp500', name: 'S&P 500', kind: 'index', spec: {} }]),
    );
    await settle(fixture);
    // the screen alerts panel under your screens (roadmap 23.17)
    (await nextRequest(http, '/api/screener/alerts')).flush(page([]));
    (await nextRequest(http, '/api/screener/alerts/events')).flush(page([]));
    await settle(fixture);
    return fixture;
  }

  function el(fixture: ComponentFixture<unknown>): HTMLElement {
    return fixture.nativeElement as HTMLElement;
  }

  function button(root: HTMLElement, text: string): HTMLButtonElement {
    const b = [...root.querySelectorAll('button')].find((x) => x.textContent?.trim() === text);
    if (!b) throw new Error(`no "${text}" button`);
    return b;
  }

  function type(root: HTMLElement, selector: string, value: string, event = 'input') {
    const input = root.querySelector<HTMLInputElement | HTMLSelectElement>(selector);
    if (!input) throw new Error(`no ${selector}`);
    input.value = value;
    input.dispatchEvent(new Event(event));
  }

  it('builds a filter, runs the screen and shows the matches', async () => {
    const fixture = await render();
    const root = el(fixture);
    type(root, '#sc-universe', 'sp500', 'change');
    button(root, 'Add a filter').click();
    fixture.detectChanges();
    const select = root.querySelector<HTMLSelectElement>('select[id^="sc-f-metric-"]')!;
    select.value = 'dividend_yield';
    select.dispatchEvent(new Event('change'));
    fixture.detectChanges();
    expect(root.textContent).toContain('In percent: 8 means 8%.');
    type(root, 'input[id^="sc-f-min-"]', '3');
    type(root, '#sc-sort', 'dividend_yield', 'change');
    fixture.detectChanges();
    button(root, 'Run screen').click();
    const size = await nextRequest(http, '/api/screener/size', 'POST');
    expect(size.request.body.spec.universe_id).toBe('sp500');
    size.flush(screenSize(120));
    const req = await nextRequest(http, '/api/screener/run', 'POST');
    expect(req.request.body).toEqual({
      as_of: null,
      spec: {
        universe_id: 'sp500',
        filters: [{ metric: 'dividend_yield', min: 0.03, max: null }],
        sort_by: 'dividend_yield',
        descending: true,
        limit: 50,
      },
    });
    req.flush(RESULT);
    await settle(fixture);
    expect(root.textContent).toContain('2 matches out of 120, on 2026-09-25.');
    expect(root.querySelector('a[href="/charts/KO.US"]')).not.toBeNull();
    const cells = [...root.querySelectorAll('td')].map((td) => td.textContent?.trim());
    expect(cells).toContain('3.1%');
    expect(cells).toContain('62.5');
  });

  it('says what is wrong and runs nothing', async () => {
    const fixture = await render();
    const root = el(fixture);
    button(root, 'Add a filter').click();
    fixture.detectChanges();
    button(root, 'Run screen').click();
    fixture.detectChanges();
    expect(root.querySelector('.errors')?.textContent).toContain('Pick a metric for each filter');
    await tick(5);
    http.expectNone((r) => r.url === '/api/screener/run');
    http.expectNone((r) => r.url === '/api/screener/size');
  });

  it('runs a large screen as a background job with progress', async () => {
    const fixture = await render();
    const root = el(fixture);
    button(root, 'Run screen').click();
    (await nextRequest(http, '/api/screener/size', 'POST')).flush(
      screenSize(4200, { use_job: true }),
    );
    const submit = await nextRequest(http, '/api/screener/jobs', 'POST');
    expect(submit.request.body).toEqual({ as_of: null, spec: { limit: 50 } });
    submit.flush({ id: 'job_1', kind: 'screen_run', status: 'queued', progress: 0 });
    await settle(fixture);
    expect(track).toHaveBeenCalledWith('job_1', expect.anything());
    expect(root.textContent).toContain('4,200 candidates');
    job.step(0.5, 'metric price (1 of 2)');
    fixture.detectChanges();
    const bar = root.querySelector<HTMLProgressElement>('progress');
    expect(bar?.value).toBe(0.5);
    expect(root.textContent).toContain('metric price (1 of 2)');
    expect(button(root, 'Cancel')).toBeTruthy();
    http.expectNone((r) => r.url === '/api/screener/run');
    job.end('succeeded');
    (await nextRequest(http, '/api/screener/jobs/job_1/result')).flush(RESULT);
    await settle(fixture);
    expect(root.textContent).toContain('2 matches out of 120, on 2026-09-25.');
    expect(root.querySelector('progress')).toBeNull();
  });

  it('cancels a running screen job', async () => {
    const fixture = await render();
    const root = el(fixture);
    button(root, 'Run screen').click();
    (await nextRequest(http, '/api/screener/size', 'POST')).flush(
      screenSize(4200, { use_job: true }),
    );
    (await nextRequest(http, '/api/screener/jobs', 'POST')).flush({
      id: 'job_1',
      kind: 'screen_run',
      status: 'queued',
      progress: 0,
    });
    await settle(fixture);
    job.step(0.2, 'metric price (1 of 2)');
    fixture.detectChanges();
    button(root, 'Cancel').click();
    (await nextRequest(http, '/api/jobs/job_1/cancel', 'POST')).flush({
      id: 'job_1',
      status: 'cancelled',
    });
    job.end('cancelled');
    await settle(fixture);
    http.expectNone((r) => r.url === '/api/screener/jobs/job_1/result');
    expect(root.textContent).toContain('Run a screen to see matches');
  });

  it('explains the candidate cap and runs nothing over it', async () => {
    const fixture = await render();
    const root = el(fixture);
    button(root, 'Run screen').click();
    (await nextRequest(http, '/api/screener/size', 'POST')).flush(
      screenSize(25000, { over_cap: true }),
    );
    await settle(fixture);
    const alert = root.querySelector('.results [role=alert]');
    expect(alert?.textContent).toContain('25,000 candidates');
    expect(alert?.textContent).toContain('10,000');
    expect(alert?.textContent).toContain('Narrow it');
    http.expectNone((r) => r.url === '/api/screener/run' || r.url === '/api/screener/jobs');
  });

  it('saves a new screen, then saves changes to it', async () => {
    const fixture = await render([]);
    const root = el(fixture);
    expect(root.textContent).toContain('No screens yet');
    type(root, '#sc-name', 'Cheap payers');
    fixture.detectChanges();
    button(root, 'Save screen').click();
    const create = await nextRequest(http, '/api/screener/screens', 'POST');
    expect(create.request.body).toEqual({ name: 'Cheap payers', spec: { limit: 50 } });
    create.flush({ ...SAVED, spec: { limit: 50 } });
    (await nextRequest(http, '/api/screener/screens')).flush(page([SAVED]));
    await settle(fixture);
    expect(root.querySelector('#screen-title')?.textContent).toContain('Cheap payers');
    // Nothing changed yet.
    expect(button(root, 'Save changes').disabled).toBe(true);

    type(root, '#sc-limit', '10');
    fixture.detectChanges();
    expect(root.textContent).toContain('Changed');
    button(root, 'Save changes').click();
    const update = await nextRequest(http, '/api/screener/screens/scr_1', 'PATCH');
    expect(update.request.body).toEqual({ name: 'Cheap payers', spec: { limit: 10 } });
    update.flush({ ...SAVED, spec: { limit: 10 } });
    (await nextRequest(http, '/api/screener/screens')).flush(page([SAVED]));
    await settle(fixture);
  });

  it('opens a saved screen into the form', async () => {
    const fixture = await render();
    const root = el(fixture);
    button(root, 'Open').click();
    (await nextRequest(http, '/api/screener/screens/scr_1')).flush(SAVED);
    await settle(fixture);
    expect(root.querySelector<HTMLInputElement>('#sc-name')!.value).toBe('Cheap payers');
    expect(root.querySelector<HTMLInputElement>('input[id^="sc-f-min-"]')!.value).toBe('4');
    expect(button(root, 'Save changes').disabled).toBe(true);
  });

  it('opens a saved screen only once the metrics are known (percent bounds)', async () => {
    const fixture = TestBed.createComponent(ScreenerPage);
    fixture.detectChanges();
    const metrics = await nextRequest(http, '/api/screener/metrics');
    (await nextRequest(http, '/api/screener/screens')).flush(page([SAVED]));
    (await nextRequest(http, '/api/universes')).flush(page([]));
    await settle(fixture);
    http.match((r) => r.url.startsWith('/api/screener/alerts')).forEach((r) => r.flush(page([])));
    await settle(fixture);
    // Without the units a stored 4% would read as 0.04 and save back as 0.0004.
    expect(button(el(fixture), 'Open').disabled).toBe(true);
    metrics.flush(METRICS);
    await settle(fixture);
    expect(button(el(fixture), 'Open').disabled).toBe(false);
  });

  it('says so when a saved screen cannot be opened', async () => {
    const fixture = await render();
    const root = el(fixture);
    const error = vi.spyOn(TestBed.inject(ToastService), 'error');
    button(root, 'Open').click();
    (await nextRequest(http, '/api/screener/screens/scr_1')).flush(
      { title: 'Not found', status: 404, detail: 'no such screen' },
      { status: 404, statusText: 'Not Found' },
    );
    await settle(fixture);
    expect(error).toHaveBeenCalledWith(
      expect.stringContaining('no such screen'),
      expect.anything(),
    );
  });

  it('deletes a screen after asking', async () => {
    const fixture = await render();
    const root = el(fixture);
    confirm.mockResolvedValueOnce(false);
    button(root, 'Delete').click();
    await tick();
    http.expectNone((r) => r.method === 'DELETE');

    confirm.mockResolvedValueOnce(true);
    button(root, 'Delete').click();
    await tick();
    expect(confirm).toHaveBeenLastCalledWith(
      expect.objectContaining({ confirmLabel: 'Delete screen', tone: 'danger' }),
    );
    (await nextRequest(http, '/api/screener/screens/scr_1', 'DELETE')).flush(null, {
      status: 204,
      statusText: 'No Content',
    });
    (await nextRequest(http, '/api/screener/screens')).flush(page([]));
    await settle(fixture);
    expect(root.textContent).toContain('No screens yet');
  });

  it('saves the screen as a snapshot universe, with the survivorship warning', async () => {
    const fixture = await render();
    const root = el(fixture);
    type(root, '#sc-name', 'Cheap payers');
    fixture.detectChanges();
    button(root, 'Save as a universe').click();
    await settle(fixture);
    const dialog = root.querySelector('dialog')!;
    expect(dialog.querySelector<HTMLInputElement>('#su-id')!.value).toBe('cheap-payers');
    // Rule mode by default, from a year ago.
    expect(dialog.querySelector('#su-start')).not.toBeNull();
    const snapshot = [...dialog.querySelectorAll<HTMLButtonElement>('[role=radio]')].find((b) =>
      b.textContent?.includes("Today's matches"),
    )!;
    snapshot.click();
    fixture.detectChanges();
    expect(dialog.textContent).toContain('Survivorship bias.');
    expect(dialog.querySelector('#su-start')).toBeNull();
    button(dialog as HTMLElement, 'Save universe').click();
    const req = await nextRequest(http, '/api/screener/universes', 'POST');
    expect(req.request.body).toEqual({
      universe_id: 'cheap-payers',
      name: 'Cheap payers',
      mode: 'snapshot',
      spec: { limit: 50 },
    });
    req.flush({
      universe: { id: 'cheap-payers', name: 'Cheap payers', kind: 'list', spec: {} },
      refresh_job: null,
      warnings: ['A snapshot has survivorship bias.'],
    });
    await settle(fixture);
    const saved = root.querySelector('section.universe')!;
    expect(saved.textContent).toContain('Cheap payers');
    expect(saved.textContent).toContain('A snapshot has survivorship bias.');
    // The saved universe links to its own page.
    expect(saved.querySelector('a[href="/universes/cheap-payers"]')?.textContent).toContain(
      'Open the universe',
    );
    expect(root.querySelector('a[href="/universes"]')).not.toBeNull();
    const lab = saved.querySelector<HTMLAnchorElement>('a[href^="/lab"]')!;
    expect(lab.getAttribute('href')).toBe('/lab?universe=cheap-payers');
  });

  it('sends a rule universe by the saved screen when it is unchanged', async () => {
    const fixture = await render();
    const root = el(fixture);
    button(root, 'Open').click();
    (await nextRequest(http, '/api/screener/screens/scr_1')).flush(SAVED);
    await settle(fixture);
    button(root, 'Save as a universe').click();
    await settle(fixture);
    const dialog = root.querySelector('dialog') as HTMLElement;
    type(dialog, '#su-start', '2025-01-02', 'change');
    button(dialog, 'Save universe').click();
    const req = await nextRequest(http, '/api/screener/universes', 'POST');
    expect(req.request.body).toEqual({
      universe_id: 'cheap-payers',
      name: 'Cheap payers',
      mode: 'rule',
      screen_id: 'scr_1',
      start: '2025-01-02',
      rebalance: 'monthly',
    });
    req.flush(problem(409, 'universe cheap-payers exists'), {
      status: 409,
      statusText: 'Conflict',
    });
    await settle(fixture);
    // The dialog stays open and says why, where the trader is looking.
    expect(dialog.querySelector('[role=alert]')?.textContent).toContain('exists');
    // Silent: no toast repeats it.
    const errors = TestBed.inject(ToastService)
      .toasts()
      .filter((t) => t.tone === 'error');
    expect(errors).toEqual([]);
  });
});
