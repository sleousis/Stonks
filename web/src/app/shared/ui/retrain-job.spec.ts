import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { type ComponentFixture, TestBed } from '@angular/core/testing';

import { provideApi } from '../../api/provide-api';
import { SessionService } from '../../core/auth/session.service';
import { JobsService } from '../../core/jobs/jobs.service';
import { ADMIN } from '../../../testing/auth-fixtures';
import { nextRequest, tick } from '../../../testing/http';
import { MODEL_ID, RETRAIN_RESULT, finishedJob } from '../../../testing/model-version-fixtures';
import { RetrainJob, retrainSummary } from './retrain-job';

describe('retrainSummary', () => {
  it('counts candidates, failures and skips', () => {
    expect(retrainSummary(RETRAIN_RESULT)).toBe('1 new candidate, 1 skipped.');
    expect(retrainSummary({ ...RETRAIN_RESULT, candidates: 0, skipped: 0, failed: 0 })).toBe(
      'No strategy needed a new fit.',
    );
  });
});

describe('RetrainJob', () => {
  let fixture: ComponentFixture<RetrainJob>;
  let http: HttpTestingController;
  let track: ReturnType<typeof vi.fn>;

  async function setup(me = ADMIN, strategyId: string | null = MODEL_ID) {
    track = vi.fn((id: string) => finishedJob(id));
    TestBed.configureTestingModule({
      providers: [
        ...provideApi(),
        provideHttpClientTesting(),
        { provide: JobsService, useValue: { track } },
      ],
    });
    http = TestBed.inject(HttpTestingController);
    const loading = TestBed.inject(SessionService).load();
    (await nextRequest(http, '/api/auth/me')).flush(me);
    await loading;
    fixture = TestBed.createComponent(RetrainJob);
    fixture.componentRef.setInput('strategyId', strategyId);
    fixture.detectChanges();
    return fixture.nativeElement as HTMLElement;
  }

  afterEach(() => http.verify());

  it('retrains one strategy, follows the job and lists what each got', async () => {
    const el = await setup();
    const finished = vi.fn();
    fixture.componentInstance.finished.subscribe(finished);
    el.querySelector<HTMLInputElement>('input[type=checkbox]')!.click();
    el.querySelector<HTMLButtonElement>('button[type=submit]')!.click();
    const post = await nextRequest(http, '/api/model-versions/retrain', 'POST');
    expect(post.request.body).toEqual({ strategy_ids: [MODEL_ID], force: true });
    post.flush({ id: 'job_r1', kind: 'model_retrain', params: {}, status: 'queued', progress: 0 });
    (await nextRequest(http, '/api/model-versions/jobs/job_r1/result')).flush(RETRAIN_RESULT);
    await tick();
    fixture.detectChanges();
    expect(track).toHaveBeenCalledWith('job_r1', expect.anything());
    expect(el.textContent).toContain('1 new candidate, 1 skipped.');
    expect(el.textContent).toContain('Candidate v3');
    expect(el.textContent).toContain('fitted 2 day(s) ago');
    expect(finished).toHaveBeenCalledWith(RETRAIN_RESULT);
  });

  it('retrains every strategy without an id', async () => {
    const el = await setup(ADMIN, null);
    el.querySelector<HTMLButtonElement>('button[type=submit]')!.click();
    const post = await nextRequest(http, '/api/model-versions/retrain', 'POST');
    expect(post.request.body).toEqual({ strategy_ids: null, force: false });
    post.flush({ id: 'job_r2', kind: 'model_retrain', params: {}, status: 'queued', progress: 0 });
    (await nextRequest(http, '/api/model-versions/jobs/job_r2/result')).flush(RETRAIN_RESULT);
    await tick();
  });

  it('is disabled for a viewer, with a note', async () => {
    const el = await setup({ ...ADMIN, role: 'viewer', scopes: ['read'] });
    expect(el.querySelector<HTMLButtonElement>('button[type=submit]')!.disabled).toBe(true);
    expect(el.querySelector('app-permission-note')).toBeTruthy();
  });
});
