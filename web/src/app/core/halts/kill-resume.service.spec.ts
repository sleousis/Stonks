import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';

import type { HaltView } from '../../api/models';
import { provideApi } from '../../api/provide-api';
import { StepUpService } from '../auth/step-up.service';
import { nextRequest, tick } from '../../../testing/http';
import { KillResumeService } from './kill-resume.service';

const KILL = { id: 7, kind: 'kill', scope: 'portfolio', portfolio_id: 'pf_paper' } as HaltView;
const OTHER = { ...KILL, id: 8 } as HaltView;

describe('KillResumeService', () => {
  let http: HttpTestingController;
  let ensure: ReturnType<typeof vi.fn>;
  let resumer: KillResumeService;

  beforeEach(() => {
    ensure = vi.fn().mockResolvedValue(true);
    TestBed.configureTestingModule({
      providers: [
        ...provideApi(),
        provideHttpClientTesting(),
        { provide: StepUpService, useValue: { ensure } },
      ],
    });
    http = TestBed.inject(HttpTestingController);
    resumer = TestBed.inject(KillResumeService);
  });

  afterEach(() => http.verify());

  it('asks for the code before it sends, so one request resumes a paper stop', async () => {
    const done = resumer.resume([KILL], { reason: 'Back', overrideChecks: false });
    const post = await nextRequest(http, '/api/halts/7/resume', 'POST');
    expect(ensure).toHaveBeenCalledWith('Resume trading');
    expect(post.request.body).toEqual({ confirmation: 'RESUME TRADING', reason: 'Back' });
    post.flush({ ...KILL, active: false });
    expect(await done).toBe(true);
    await tick();
    // No second try: the code came first.
    http.expectNone('/api/halts/7/resume');
  });

  it('sends nothing when the code is cancelled', async () => {
    ensure.mockResolvedValue(false);
    expect(await resumer.resume([KILL], { reason: 'Back', overrideChecks: false })).toBe(false);
    http.expectNone('/api/halts/7/resume');
  });

  it('resumes each switch once after one code, passing an override', async () => {
    const done = resumer.resume([KILL, OTHER], { reason: 'Back', overrideChecks: true });
    const first = await nextRequest(http, '/api/halts/7/resume', 'POST');
    expect(first.request.body).toEqual({
      confirmation: 'RESUME TRADING',
      reason: 'Back',
      override_checks: true,
    });
    first.flush({ ...KILL, active: false });
    (await nextRequest(http, '/api/halts/8/resume', 'POST')).flush({ ...OTHER, active: false });
    expect(await done).toBe(true);
    expect(ensure).toHaveBeenCalledTimes(1);
  });
});
